import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from .dcard import classify_article


SEARCH_QUERIES = [
    "醫美診所 糾紛",
    "醫美 投訴 公審",
    "整形 失敗 後悔",
    "診所 醫療疏失",
    "醫師 糾紛 投訴",
    "醫美 詐騙",
]


async def scrape_google_news() -> list[dict]:
    articles = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=15) as client:
        for query in SEARCH_QUERIES:
            try:
                url = "https://news.google.com/rss/search"
                resp = await client.get(
                    url,
                    params={"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code != 200:
                    continue

                root = ET.fromstring(resp.content)
                items = root.findall(".//item")

                for item in items[:10]:
                    link = item.findtext("link", "")
                    if not link or link in seen_urls:
                        continue
                    seen_urls.add(link)

                    title = item.findtext("title", "")
                    description = item.findtext("description", "")
                    category, neg_kws = classify_article(title, description)

                    if not neg_kws:
                        continue

                    pub_date = item.findtext("pubDate", "")
                    try:
                        published_at = parsedate_to_datetime(pub_date)
                    except Exception:
                        published_at = datetime.now(timezone.utc)

                    articles.append({
                        "source": "google_news",
                        "title": title,
                        "content_snippet": description[:500],
                        "url": link,
                        "author": item.findtext("source", ""),
                        "category": category,
                        "negative_keywords": neg_kws,
                        "comment_count": 0,
                        "published_at": published_at.isoformat(),
                    })
            except Exception as e:
                print(f"Google News 爬取失敗 [{query}]: {e}")

    return articles
