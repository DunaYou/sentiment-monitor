import asyncio
import os
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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


@app.get("/api/articles")
async def get_articles(category: str = None, limit: int = 50, offset: int = 0):
    query = supabase.table("articles").select("*").order("published_at", desc=True).range(offset, offset + limit - 1)
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return {"articles": result.data or []}
