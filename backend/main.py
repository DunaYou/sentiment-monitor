import asyncio
import os
import httpx
import resend
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from supabase import create_client, Client

from scrapers.dcard import scrape_dcard
from scrapers.google_news import scrape_google_news
from scrapers.ptt import scrape_ptt
from scrapers.threads import scrape_threads

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
ADMIN_EMAIL = "dunayou.ef@gmail.com"
BACKEND_URL = "https://sentiment-monitor-yik9.onrender.com"

resend.api_key = RESEND_API_KEY

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def send_line_notification(user_id: str, article: dict):
    category_label = "🔴 醫美" if article["category"] == "medical_beauty" else "🟡 其他醫療"
    source_label = {"dcard": "Dcard", "ptt": "PTT", "google_news": "Google 新聞", "threads": "Threads"}.get(article["source"], article["source"])
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
            result = supabase.table("articles").upsert(
                article,
                on_conflict="url",
                ignore_duplicates=True
            ).execute()

            if result.data:
                new_count += 1
                saved = result.data[0]

                # 查詢有訂閱該類別的用戶
                col = "notify_medical_beauty" if saved["category"] == "medical_beauty" else "notify_other_medical"
                users = supabase.table("user_settings").select("line_user_id").eq(col, True).not_.is_("line_user_id", "null").execute()

                tasks = [send_line_notification(u["line_user_id"], saved) for u in (users.data or []) if u.get("line_user_id")]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            print(f"儲存失敗: {e}")

    return new_count


@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.get("/sentiment-scan")
async def sentiment_scan(x_scan_secret: str = Header(default="")):
    if SCAN_SECRET and x_scan_secret != SCAN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = await asyncio.gather(
        scrape_dcard(),
        scrape_google_news(),
        scrape_ptt(),
        scrape_threads(),
        return_exceptions=True
    )

    all_articles = []
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)
        else:
            print(f"爬蟲錯誤: {r}")

    new_count = await save_and_notify(all_articles)

    return {
        "status": "ok",
        "scanned": len(all_articles),
        "new": new_count
    }


@app.get("/api/check-approval")
async def check_approval(user_id: str):
    rec = supabase.table("pending_registrations").select("approved_at, name").eq("user_id", user_id).execute()
    if not rec.data:
        return {"approved": True}  # 不在 pending 表 = 原有帳號，直接放行
    row = rec.data[0]
    return {"approved": row["approved_at"] is not None, "name": row.get("name", "")}


@app.get("/api/articles")
async def get_articles(category: str = None, limit: int = 50, offset: int = 0):
    query = supabase.table("articles").select("*").order("published_at", desc=True).range(offset, offset + limit - 1)
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return {"articles": result.data or []}


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str


@app.post("/api/register")
async def register_user(body: RegisterRequest):
    # 用 Admin API 建帳號
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

    # 建立 pending 記錄，取得 approval_token
    rec = supabase.table("pending_registrations").insert({
        "user_id": user_id,
        "email": body.email,
        "name": body.name,
    }).execute()

    token = rec.data[0]["approval_token"]
    approve_url = f"{BACKEND_URL}/approve-user?token={token}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 寄審核信給管理員
    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": ADMIN_EMAIL,
        "subject": f"【輿情系統】新用戶申請：{body.name}",
        "html": f"""
        <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px">
          <h2 style="color:#173B2F">新用戶申請審核</h2>
          <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <tr><td style="padding:8px;color:#888;width:80px">姓名</td><td style="padding:8px;font-weight:600">{body.name}</td></tr>
            <tr style="background:#f9f9f9"><td style="padding:8px;color:#888">Email</td><td style="padding:8px">{body.email}</td></tr>
            <tr><td style="padding:8px;color:#888">時間</td><td style="padding:8px">{ts}</td></tr>
          </table>
          <a href="{approve_url}" style="display:inline-block;background:#173B2F;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:8px">
            ✅ 批准帳號
          </a>
          <p style="color:#aaa;font-size:12px;margin-top:24px">若非你認識的人，忽略此信即可，對方無法登入。</p>
        </div>
        """
    })

    return {"status": "pending"}


@app.get("/approve-user", response_class=HTMLResponse)
async def approve_user(token: str):
    # 查 pending 記錄
    rec = supabase.table("pending_registrations").select("*").eq("approval_token", token).is_("approved_at", "null").execute()
    if not rec.data:
        return HTMLResponse("<h2>連結無效或已使用過</h2>", status_code=400)

    row = rec.data[0]

    # 解除帳號凍結
    async with httpx.AsyncClient() as client:
        await client.put(
            f"{SUPABASE_URL}/auth/v1/admin/users/{row['user_id']}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY},
            json={"banned_until": None}
        )

    # 標記已審核
    supabase.table("pending_registrations").update(
        {"approved_at": datetime.now(timezone.utc).isoformat()}
    ).eq("approval_token", token).execute()

    # 通知管理員帳號已啟用（Resend 免費版只能寄給帳號本人）
    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": ADMIN_EMAIL,
        "subject": f"【輿情系統】{row['name']} 的帳號已啟用，請通知對方",
        "html": f"""
        <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px">
          <h2 style="color:#1B4F82">帳號啟用完成 ✅</h2>
          <p style="color:#555;margin:8px 0 20px">以下用戶帳號已成功啟用，請自行通知對方可以登入了。</p>
          <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <tr><td style="padding:8px;color:#888;width:60px">姓名</td><td style="padding:8px;font-weight:600">{row['name']}</td></tr>
            <tr style="background:#f9f9f9"><td style="padding:8px;color:#888">Email</td><td style="padding:8px">{row['email']}</td></tr>
          </table>
          <p style="color:#555;font-size:13px">登入網址：<a href="https://duna-sentiment.surge.sh" style="color:#1B4F82">https://duna-sentiment.surge.sh</a></p>
        </div>
        """
    })

    return HTMLResponse(f"""
    <html><head><meta charset="utf-8"><style>
      body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f4f6f9}}
      .card{{background:#fff;border-radius:16px;padding:48px 40px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
      h2{{color:#173B2F}} p{{color:#666}} a{{color:#173B2F;font-weight:600}}
    </style></head><body>
    <div class="card">
      <div style="font-size:48px">✅</div>
      <h2>{row['name']} 的帳號已啟用</h2>
      <p>{row['email']} 現在可以登入系統了。</p>
    </div>
    </body></html>
    """)
