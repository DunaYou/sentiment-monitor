import asyncio
import os
import httpx
import resend
from datetime import datetime, timezone
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from supabase import create_client, Client

from scrapers.dcard import scrape_dcard
from scrapers.google_news import scrape_google_news
from scrapers.ptt import scrape_ptt
from scrapers.threads import scrape_threads
from scrapers.mobile01 import scrape_mobile01

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
SCAN_SECRET = os.environ.get("SCAN_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_EMAIL = "dunayou.ef@gmail.com"
BACKEND_URL = "https://sentiment-monitor-yik9.onrender.com"

resend.api_key = RESEND_API_KEY

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ── AI 分類 ────────────────────────────────────────────────
AI_LABEL_PROMPT = """你是醫療輿情分析師。請根據文章標題與摘要，將這篇文章分類成以下其中一類，只回傳分類名稱，不要說明：

- 醫療糾紛（有病人投訴、起訴、賠償、死亡、醫療疏失、公審等）
- 負評投訴（有明顯不滿但未到糾紛程度，如退款糾紛、服務差、亂收費）
- 一般討論（問診推薦、比較診所、中性分享）
- 正面評價（推薦、稱讚、好評）
- 新聞報導（媒體新聞、政策、法規）"""


async def classify_with_ai(title: str, snippet: str) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 20,
                    "system": AI_LABEL_PROMPT,
                    "messages": [{"role": "user", "content": f"標題：{title}\n摘要：{snippet[:200]}"}],
                }
            )
        if resp.status_code == 200:
            return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"AI 分類失敗: {e}")
    return ""


# ── LINE 通知 ───────────────────────────────────────────────
async def send_line_notification(user_id: str, article: dict):
    category_label = "🔴 醫美" if article["category"] == "medical_beauty" else "🟡 其他醫療"
    source_label = {"dcard": "Dcard", "ptt": "PTT", "google_news": "Google 新聞", "threads": "Threads", "mobile01": "Mobile01"}.get(article["source"], article["source"])
    keywords = "、".join(article.get("negative_keywords", [])[:3])

    message = {
        "type": "flex",
        "altText": f"[輿情警示] {article['title'][:30]}",
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": f"{category_label}  {source_label}", "color": "#ffffff", "size": "sm"}],
                "backgroundColor": "#173B2F" if article["category"] == "medical_beauty" else "#555555",
                "paddingAll": "12px"
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": article["title"][:60], "wrap": True, "weight": "bold", "size": "sm"},
                    {"type": "text", "text": f"關鍵字：{keywords}", "color": "#e74c3c", "size": "xs", "margin": "sm"},
                    {"type": "text", "text": article.get("content_snippet", "")[:80], "wrap": True, "size": "xs", "color": "#555555", "margin": "sm"}
                ],
                "paddingAll": "12px"
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "button", "action": {"type": "uri", "label": "查看原文", "uri": article["url"]}, "style": "primary", "color": "#173B2F", "height": "sm"}],
                "paddingAll": "8px"
            }
        }
    }

    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"to": user_id, "messages": [message]}
        )


async def save_and_notify(articles: list[dict]):
    if not articles:
        return 0

    new_count = 0
    for article in articles:
        try:
            # AI 分類
            if ANTHROPIC_API_KEY and not article.get("ai_label"):
                article["ai_label"] = await classify_with_ai(
                    article.get("title", ""),
                    article.get("content_snippet", "")
                )

            result = supabase.table("articles").upsert(
                article,
                on_conflict="url",
                ignore_duplicates=True
            ).execute()

            if result.data:
                new_count += 1
                saved = result.data[0]

                col = "notify_medical_beauty" if saved["category"] == "medical_beauty" else "notify_other_medical"
                users = supabase.table("user_settings").select("line_user_id").eq(col, True).not_.is_("line_user_id", "null").execute()

                tasks = [send_line_notification(u["line_user_id"], saved) for u in (users.data or []) if u.get("line_user_id")]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            print(f"儲存失敗: {e}")

    return new_count


# ── 掃描 ────────────────────────────────────────────────────
@app.get("/ping")
def ping():
    return {"status": "ok"}


async def _run_scan():
    results = await asyncio.gather(
        scrape_dcard(),
        scrape_google_news(),
        scrape_ptt(),
        scrape_threads(),
        scrape_mobile01(),
        return_exceptions=True
    )
    all_articles = []
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)
        else:
            print(f"爬蟲錯誤: {r}")
    await save_and_notify(all_articles)
    print(f"掃描完成，共 {len(all_articles)} 筆")


@app.get("/sentiment-scan")
async def sentiment_scan(background_tasks: BackgroundTasks, x_scan_secret: str = Header(default="")):
    if SCAN_SECRET and x_scan_secret != SCAN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    background_tasks.add_task(_run_scan)
    return {"status": "accepted"}


# ── 關鍵字搜尋 ───────────────────────────────────────────────
@app.get("/api/search")
async def search_articles(q: str, sources: str = "dcard,ptt,google_news,mobile01,threads"):
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="關鍵字太短")

    keywords = [q.strip()]
    source_list = [s.strip() for s in sources.split(",")]

    tasks = []
    labels = []
    if "dcard" in source_list:
        tasks.append(scrape_dcard(keywords=keywords))
        labels.append("dcard")
    if "ptt" in source_list:
        tasks.append(_search_ptt(q))
        labels.append("ptt")
    if "google_news" in source_list:
        tasks.append(_search_google_news(q))
        labels.append("google_news")
    if "mobile01" in source_list:
        tasks.append(scrape_mobile01(keywords=keywords))
        labels.append("mobile01")
    if "threads" in source_list:
        tasks.append(_search_threads(q))
        labels.append("threads")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles = []
    counts = {}
    for label, result in zip(labels, results):
        items = result if isinstance(result, list) else []
        counts[label] = len(items)
        for item in items:
            item["source"] = label
        all_articles.extend(items)

    # 對搜尋結果做 AI 分類（批次，最多 20 篇）
    if ANTHROPIC_API_KEY:
        ai_tasks = [
            classify_with_ai(a.get("title", ""), a.get("content_snippet", ""))
            for a in all_articles[:20]
        ]
        ai_labels = await asyncio.gather(*ai_tasks, return_exceptions=True)
        for i, label in enumerate(ai_labels):
            if isinstance(label, str):
                all_articles[i]["ai_label"] = label

    all_articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)

    return {
        "query": q,
        "total": len(all_articles),
        "counts": counts,
        "articles": all_articles,
    }


async def _search_ptt(query: str) -> list[dict]:
    from scrapers.dcard import classify_article
    articles = []
    boards = ["MedicalLaw", "doctor", "Gossiping", "beauty", "MakeUp"]
    seen_urls = set()

    async with httpx.AsyncClient(timeout=15, cookies={"over18": "1"}) as client:
        for board in boards:
            try:
                resp = await client.get(
                    f"https://www.ptt.cc/bbs/{board}/search?q={query}",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code != 200:
                    continue

                from html.parser import HTMLParser

                class PTTSearchParser(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.posts = []
                        self._in_title = False
                        self._current_href = None

                    def handle_starttag(self, tag, attrs):
                        attrs_dict = dict(attrs)
                        if tag == "a":
                            href = attrs_dict.get("href", "")
                            if "/bbs/" in href and ".html" in href:
                                self._current_href = "https://www.ptt.cc" + href
                                self._in_title = True

                    def handle_data(self, data):
                        if self._in_title and data.strip() and self._current_href:
                            self.posts.append({"url": self._current_href, "title": data.strip()})
                            self._in_title = False
                            self._current_href = None

                parser = PTTSearchParser()
                parser.feed(resp.text)

                for post in parser.posts[:10]:
                    url = post["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    category, neg_kws = classify_article(post["title"], "")
                    articles.append({
                        "source": "ptt",
                        "title": post["title"],
                        "content_snippet": f"PTT/{board}",
                        "url": url,
                        "author": "",
                        "category": category,
                        "negative_keywords": neg_kws,
                        "comment_count": 0,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    })
            except Exception as e:
                print(f"PTT 搜尋失敗 [{board}]: {e}")

    return articles


async def _search_google_news(query: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from scrapers.dcard import classify_article
    articles = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                "https://news.google.com/rss/search",
                params={"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:20]:
                    link = item.findtext("link", "")
                    if not link or link in seen_urls:
                        continue
                    seen_urls.add(link)
                    title = item.findtext("title", "")
                    description = item.findtext("description", "")
                    category, neg_kws = classify_article(title, description)
                    pub_date = item.findtext("pubDate", "")
                    try:
                        published_at = parsedate_to_datetime(pub_date).isoformat()
                    except Exception:
                        published_at = datetime.now(timezone.utc).isoformat()
                    articles.append({
                        "source": "google_news",
                        "title": title,
                        "content_snippet": description[:500],
                        "url": link,
                        "author": item.findtext("source", ""),
                        "category": category,
                        "negative_keywords": neg_kws,
                        "comment_count": 0,
                        "published_at": published_at,
                    })
        except Exception as e:
            print(f"Google News 搜尋失敗: {e}")

    return articles


async def _search_threads(query: str) -> list[dict]:
    import asyncio
    from scrapers.threads import _ddg_search
    from scrapers.dcard import classify_article
    articles = []
    seen_urls = set()

    results = await asyncio.to_thread(_ddg_search, query)
    for item in results:
        url = item.get("href", "")
        if not url or "threads.net" not in url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = item.get("title", "")
        description = item.get("body", "")
        category, neg_kws = classify_article(title, description)
        articles.append({
            "source": "threads",
            "title": title or query,
            "content_snippet": description[:500],
            "url": url,
            "author": "",
            "category": category,
            "negative_keywords": neg_kws,
            "comment_count": 0,
            "published_at": datetime.now(timezone.utc).isoformat(),
        })

    return articles


# ── 查詢文章 ────────────────────────────────────────────────
@app.get("/api/check-approval")
async def check_approval(user_id: str):
    rec = supabase.table("pending_registrations").select("approved_at, name").eq("user_id", user_id).execute()
    if not rec.data:
        return {"approved": True}
    row = rec.data[0]
    return {"approved": row["approved_at"] is not None, "name": row.get("name", "")}


@app.get("/api/articles")
async def get_articles(category: str = None, ai_label: str = None, limit: int = 50, offset: int = 0):
    query = supabase.table("articles").select("*").order("published_at", desc=True).range(offset, offset + limit - 1)
    if category:
        query = query.eq("category", category)
    if ai_label:
        query = query.eq("ai_label", ai_label)
    result = query.execute()
    return {"articles": result.data or []}


# ── 帳號管理 ────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str


@app.post("/api/register")
async def register_user(body: RegisterRequest):
    async with httpx.AsyncClient() as client:
        create_resp = await client.post(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY},
            json={"email": body.email, "password": body.password, "email_confirm": True}
        )

        if create_resp.status_code == 422:
            raise HTTPException(status_code=409, detail="此 Email 已被使用")
        if create_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="建立帳號失敗")

        user_id = create_resp.json()["id"]

    rec = supabase.table("pending_registrations").insert({
        "user_id": user_id,
        "email": body.email,
        "name": body.name,
    }).execute()

    token = rec.data[0]["approval_token"]
    approve_url = f"{BACKEND_URL}/approve-user?token={token}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    all_users = supabase.table("pending_registrations").select("name, email, approved_at").order("created_at").execute()
    user_rows = ""
    for u in (all_users.data or []):
        status = "✅ 已啟用" if u.get("approved_at") else "⏳ 待審核"
        bg = "" if u.get("approved_at") else 'style="background:#fffbe6"'
        user_rows += f'<tr {bg}><td style="padding:6px 8px">{u["name"]}</td><td style="padding:6px 8px;color:#666">{u["email"]}</td><td style="padding:6px 8px">{status}</td></tr>'

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": ADMIN_EMAIL,
        "subject": f"【輿情系統】新用戶申請：{body.name}",
        "html": f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px">
          <h2 style="color:#1B4F82">新用戶申請審核</h2>
          <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <tr><td style="padding:8px;color:#888;width:80px">姓名</td><td style="padding:8px;font-weight:600">{body.name}</td></tr>
            <tr style="background:#f9f9f9"><td style="padding:8px;color:#888">Email</td><td style="padding:8px">{body.email}</td></tr>
            <tr><td style="padding:8px;color:#888">時間</td><td style="padding:8px">{ts}</td></tr>
          </table>
          <a href="{approve_url}" style="display:inline-block;background:#1B4F82;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:8px">
            ✅ 批准帳號
          </a>
          <hr style="border:none;border-top:1px solid #eee;margin:28px 0 16px">
          <p style="color:#555;font-size:13px;font-weight:600;margin-bottom:8px">目前所有帳號（共 {len(all_users.data or [])} 人）</p>
          <table style="border-collapse:collapse;width:100%;font-size:13px">
            <tr style="background:#f4f6f9"><th style="padding:6px 8px;text-align:left;color:#888">姓名</th><th style="padding:6px 8px;text-align:left;color:#888">Email</th><th style="padding:6px 8px;text-align:left;color:#888">狀態</th></tr>
            {user_rows}
          </table>
          <p style="color:#aaa;font-size:12px;margin-top:24px">若非你認識的人，忽略此信即可，對方無法登入。</p>
        </div>
        """
    })

    return {"status": "pending"}


@app.get("/approve-user", response_class=HTMLResponse)
async def approve_user(token: str):
    rec = supabase.table("pending_registrations").select("*").eq("approval_token", token).is_("approved_at", "null").execute()
    if not rec.data:
        return HTMLResponse("<h2>連結無效或已使用過</h2>", status_code=400)

    row = rec.data[0]

    async with httpx.AsyncClient() as client:
        await client.put(
            f"{SUPABASE_URL}/auth/v1/admin/users/{row['user_id']}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY},
            json={"banned_until": None}
        )

    supabase.table("pending_registrations").update(
        {"approved_at": datetime.now(timezone.utc).isoformat()}
    ).eq("approval_token", token).execute()

    email_note = ""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": "onboarding@resend.dev",
                    "to": [ADMIN_EMAIL],
                    "subject": f"[輿情系統] {row['name']} 的帳號已啟用，請通知對方",
                    "html": f"<p><b>{row['name']}</b>（{row['email']}）的帳號已啟用，請通知對方至 https://duna-sentiment.surge.sh 登入。</p>"
                }
            )
        email_note = "✉️ 通知信已發送" if r.status_code == 200 else f"⚠️ 寄信失敗（{r.status_code}）"
    except Exception as e:
        email_note = f"⚠️ 例外：{e}"

    return HTMLResponse(f"""
    <html><head><meta charset="utf-8"><style>
      body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f4f6f9}}
      .card{{background:#fff;border-radius:16px;padding:48px 40px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
      h2{{color:#1B4F82}} p{{color:#666}}
    </style></head><body>
    <div class="card">
      <div style="font-size:48px">✅</div>
      <h2>{row['name']} 的帳號已啟用</h2>
      <p>{row['email']} 現在可以登入系統了。</p>
      <p style="font-size:13px;color:#888;margin-top:16px">{email_note}</p>
    </div>
    </body></html>
    """)
