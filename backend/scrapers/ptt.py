import httpx
from datetime import datetime, timezone
from .dcard import classify_article


BOARDS = ["MedicalLaw", "doctor", "Gossiping", "beauty"]
SEARCH_KEYWORDS = ["醫美", "診所", "醫療糾紛", "整形", "玻尿酸"]


async def scrape_ptt() -> list[dict]:
    articles = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=15, cookies={"over18": "1"}) as client:
        for board in BOARDS:
            try:
                resp = await client.get(
                    f"https://www.ptt.cc/bbs/{board}/index.html",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code != 200:
                    continue

                # 簡單解析文章列表
                from html.parser import HTMLParser

                class PTTParser(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.posts = []
                        self._in_title = False
                        self._current = {}

                    def handle_starttag(self, tag, attrs):
                        attrs_dict = dict(attrs)
                        if tag == "div" and "r-ent" in attrs_dict.get("class", ""):
                            self._current = {}
                        if tag == "a" and self._current is not None:
                            href = attrs_dict.get("href", "")
                            if "/bbs/" in href and href not in seen_urls:
                                self._current["url"] = "https://www.ptt.cc" + href
                                self._in_title = True

                    def handle_data(self, data):
                        if self._in_title and data.strip():
                            self._current["title"] = data.strip()
                            self._in_title = False
                            if self._current.get("url"):
                                self.posts.append(dict(self._current))

                parser = PTTParser()
                parser.feed(resp.text)

                for post in parser.posts[:20]:
                    url = post.get("url", "")
                    title = post.get("title", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    category, neg_kws = classify_article(title, "")
                    if category == "other":
                        continue

                    articles.append({
                        "source": "ptt",
                        "title": title,
                        "content_snippet": f"PTT/{board}",
                        "url": url,
                        "author": "",
                        "category": category,
                        "negative_keywords": neg_kws,
                        "comment_count": 0,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    })

            except Exception as e:
                print(f"PTT 爬取失敗 [{board}]: {e}")

    return articles
