import asyncio
import os
from datetime import datetime, timezone
from .dcard import classify_article


SEARCH_QUERIES = [
    "醫美糾紛",
    "診所公審",
    "整形失敗",
    "醫美詐騙",
    "玻尿酸 後悔",
]


def _ddg_search(query: str) -> list[dict]:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(f"site:threads.net {query}", max_results=10))
    except Exception as e:
        print(f"Threads DDG 搜尋失敗 [{query}]: {e}")
        return []


async def scrape_threads() -> list[dict]:
    articles = []
    seen_urls = set()

    for query in SEARCH_QUERIES:
        results = await asyncio.to_thread(_ddg_search, query)

        for item in results:
            url = item.get("href", "")
            if not url or "threads.net" not in url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = item.get("title", "")
            description = item.get("body", "")
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

    return articles
