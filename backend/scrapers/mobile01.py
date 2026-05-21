import httpx
from datetime import datetime, timezone
from html.parser import HTMLParser
from .dcard import classify_article


SEARCH_QUERIES = [
    "醫美糾紛",
    "診所 投訴",
    "整形 失敗",
    "醫療疏失",
    "醫美詐騙",
]


class Mobile01ResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_title = False
        self._current_href = None
        self._current_title = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a":
            href = attrs_dict.get("href", "")
            cls = attrs_dict.get("class", "")
            if "topicTitle" in cls or "topic-title" in cls or "/talk/" in href:
                self._current_href = href if href.startswith("http") else f"https://www.mobile01.com{href}"
                self._in_title = True

    def handle_data(self, data):
        if self._in_title and data.strip():
            self._current_title = data.strip()
            self._in_title = False

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href and self._current_title:
            self.results.append({"url": self._current_href, "title": self._current_title})
            self._current_href = None
            self._current_title = None


async def scrape_mobile01(keywords: list[str] = None) -> list[dict]:
    queries = keywords if keywords else SEARCH_QUERIES
    articles = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=15) as client:
        for query in queries:
            try:
                resp = await client.get(
                    "https://www.mobile01.com/search.php",
                    params={"q": query, "type": "post"},
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
                )
                if resp.status_code != 200:
                    continue

                parser = Mobile01ResultParser()
                parser.feed(resp.text)

                for item in parser.results[:15]:
                    url = item["url"]
                    title = item["title"]
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    category, neg_kws = classify_article(title, "")
                    if category == "other":
                        continue

                    articles.append({
                        "source": "mobile01",
                        "title": title,
                        "content_snippet": f"Mobile01 搜尋：{query}",
                        "url": url,
                        "author": "",
                        "category": category,
                        "negative_keywords": neg_kws,
                        "comment_count": 0,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    })

            except Exception as e:
                print(f"Mobile01 爬取失敗 [{query}]: {e}")

    return articles
