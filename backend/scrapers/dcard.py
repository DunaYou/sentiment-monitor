import httpx
from datetime import datetime, timezone


MEDICAL_BEAUTY_KEYWORDS = [
    "醫美", "整形", "玻尿酸", "肉毒", "雙眼皮", "隆鼻", "抽脂",
    "拉皮", "微整形", "皮秒", "雷射", "醫美診所", "美容外科",
    "隆胸", "縮鼻", "埋線", "音波拉提", "熱瑪吉"
]

NEGATIVE_KEYWORDS = [
    "糾紛", "公審", "投訴", "失敗", "後悔", "詐騙",
    "副作用", "毀容", "起訴", "賠償", "出事", "黑心", "被騙",
    "醫療疏失", "疤痕", "感染", "不退款", "亂收費", "起底",
    "爛醫生", "爛診所"
]

OTHER_MEDICAL_KEYWORDS = [
    "診所", "醫療糾紛", "醫師", "醫療疏失", "公審醫生", "爛醫生"
]


def classify_article(title: str, content: str) -> tuple[str, list[str]]:
    text = (title + " " + (content or "")).lower()
    matched_negative = [kw for kw in NEGATIVE_KEYWORDS if kw in text]

    for kw in MEDICAL_BEAUTY_KEYWORDS:
        if kw in text:
            return "medical_beauty", matched_negative

    for kw in OTHER_MEDICAL_KEYWORDS:
        if kw in text:
            return "other_medical", matched_negative

    return "other", matched_negative


async def scrape_dcard(keywords: list[str] = None) -> list[dict]:
    if keywords is None:
        keywords = MEDICAL_BEAUTY_KEYWORDS[:5] + OTHER_MEDICAL_KEYWORDS[:3]

    articles = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=15) as client:
        for keyword in keywords:
            try:
                resp = await client.get(
                    "https://www.dcard.tw/service/api/v2/search/posts",
                    params={"query": keyword, "limit": 20},
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code != 200:
                    continue

                posts = resp.json()
                for post in posts:
                    url = f"https://www.dcard.tw/f/{post.get('forumAlias', 'all')}/p/{post.get('id')}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = post.get("title", "")
                    excerpt = post.get("excerpt", "")
                    category, neg_kws = classify_article(title, excerpt)

                    if not neg_kws:
                        continue

                    created_at = post.get("createdAt", "")
                    try:
                        published_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except Exception:
                        published_at = datetime.now(timezone.utc)

                    articles.append({
                        "source": "dcard",
                        "title": title,
                        "content_snippet": excerpt[:500],
                        "url": url,
                        "author": post.get("school", ""),
                        "category": category,
                        "negative_keywords": neg_kws,
                        "comment_count": post.get("commentCount", 0),
                        "published_at": published_at.isoformat(),
                    })
            except Exception as e:
                print(f"Dcard 爬取失敗 [{keyword}]: {e}")

    return articles
