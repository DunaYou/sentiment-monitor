import httpx
import os
from datetime import datetime, timezone
from .dcard import classify_article, MEDICAL_BEAUTY_KEYWORDS, NEGATIVE_KEYWORDS


FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "fc-9feb4dac90a04b18b0cabbfec23c3acc")
SEARCH_QUERIES = [
    "醫美糾紛",
    "診所公審",
    "整形失敗",
    "醫美詐騙",
    "玻尿酸 後悔",
]


async def scrape_threads() -> list[dict]:
    articles = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=30) as client:
        for query in SEARCH_QUERIES:
            try:
                # 用 Firecrawl 搜尋 Threads 公開頁面
                resp = await client.post(
                    "https://api.firecrawl.dev/v1/search",
                    headers={
                        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "query": f"site:threads.net {query}",
                        "limit": 10,
                        "lang": "zh",
                        "country": "tw"
                    }
                )
                if resp.status_code != 200:
                    continue

                data = resp.json()
                results = data.get("data", [])

                for item in results:
                    url = item.get("url", "")
                    if not url or "threads.net" not in url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = item.get("title", "")
                    description = item.get("description", "")
                    category, neg_kws = classify_article(title, description)

                    if not neg_kws:
                        continue

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

            except Exception as e:
                print(f"Threads 爬取失敗 [{query}]: {e}")

    return articles
