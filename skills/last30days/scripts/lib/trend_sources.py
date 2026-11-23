"""Text-source registry and pure parsers for the standalone trend sweep.

URL templates are resolved once per run. Add a source by adding a Source row
using an existing format; parsers never perform I/O.
"""

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from . import atom_read, geeknews, producthunt

MAX_BODY_BYTES = 5 * 1024 * 1024
GROUPS = ("overseas", "korea", "video")
XML_FORMATS = frozenset({"feed", "producthunt", "geeknews"})


@dataclass(frozen=True)
class Source:
    id: str
    group: str
    url: str
    format: str
    limit: int


SOURCES = (
    Source("producthunt", "overseas", "https://www.producthunt.com/feed", "producthunt", 50),
    Source("hn_front_page", "overseas", "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={limit}", "hn", 30),
    Source("hn_week_top", "overseas", "https://hn.algolia.com/api/v1/search?tags=story&numericFilters=created_at_i%3E{week_ts}&hitsPerPage={limit}", "hn", 100),
    Source("hn_show", "overseas", "https://hn.algolia.com/api/v1/search?tags=show_hn&numericFilters=created_at_i%3E{week_ts}&hitsPerPage={limit}", "hn", 60),
    Source("github_trending_weekly", "overseas", "https://github.com/trending?since=weekly", "github", 100),
    Source("github_trending_daily", "overseas", "https://github.com/trending?since=daily", "github", 100),
    Source("huggingface_models", "overseas", "https://huggingface.co/api/models?sort=trendingScore&limit={limit}", "hf_models", 40),
    Source("huggingface_spaces", "overseas", "https://huggingface.co/api/spaces?sort=trendingScore&limit={limit}", "hf_spaces", 40),
    Source("huggingface_daily_papers", "overseas", "https://huggingface.co/api/daily_papers?limit={limit}", "hf_papers", 50),
    Source("techmeme", "overseas", "https://www.techmeme.com/feed.xml", "feed", 15),
    Source("lobsters", "overseas", "https://lobste.rs/hottest.json", "lobsters", 25),
    Source("devto_top_week", "overseas", "https://dev.to/api/articles?top=7&per_page={limit}", "devto", 40),
    Source("devto_ai_week", "overseas", "https://dev.to/api/articles?tag=ai&top=7&per_page={limit}", "devto", 40),
    Source("techcrunch_ai", "overseas", "https://techcrunch.com/category/artificial-intelligence/feed/", "feed", 20),
    Source("techcrunch_startups", "overseas", "https://techcrunch.com/category/startups/feed/", "feed", 20),
    Source("google_trends_us", "overseas", "https://trends.google.com/trending/rss?geo=US", "feed", 10),
    Source("wikipedia_en_top", "overseas", "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{wiki_day}", "wikipedia", 50),
    Source("appstore_us_top_free", "overseas", "https://rss.marketingtools.apple.com/api/v2/us/apps/top-free/{limit}/apps.json", "apple", 50),
    Source("mastodon_links", "overseas", "https://mastodon.social/api/v1/trends/links?limit={limit}", "mastodon_links", 10),
    Source("mastodon_tags", "overseas", "https://mastodon.social/api/v1/trends/tags?limit={limit}", "mastodon_tags", 10),
    Source("reddit_sideproject", "overseas", "https://arctic-shift.photon-reddit.com/api/posts/search?limit={limit}&sort=desc&subreddit=SideProject", "reddit", 100),
    Source("reddit_saas", "overseas", "https://arctic-shift.photon-reddit.com/api/posts/search?limit={limit}&sort=desc&subreddit=SaaS", "reddit", 100),
    Source("knowyourmeme_news", "video", "https://knowyourmeme.com/newsfeed.rss", "feed", 5),
    Source("knowyourmeme_memes", "video", "https://knowyourmeme.com/memes.rss", "feed", 16),
    Source("geeknews", "korea", "https://news.hada.io/rss/news", "geeknews", 20),
    Source("yozm_it", "korea", "https://yozm.wishket.com/magazine/feed/", "feed", 30),
    Source("platum", "korea", "https://platum.kr/feed", "feed", 30),
    Source("venturesquare", "korea", "https://www.venturesquare.net/feed", "feed", 30),
    Source("byline_network", "korea", "https://byline.network/feed/", "feed", 20),
    Source("aitimes", "korea", "https://www.aitimes.com/rss/allArticle.xml", "feed", 50),
    Source("etnews", "korea", "https://rss.etnews.com/Section901.xml", "feed", 30),
    Source("zdnet_korea", "korea", "https://feeds.feedburner.com/zdkorea", "feed", 30),
    Source("bloter", "korea", "https://www.bloter.net/rss/allArticle.xml", "feed", 50),
    Source("tech42", "korea", "https://www.tech42.co.kr/feed/", "feed", 10),
    Source("startuprecipe", "korea", "https://startuprecipe.co.kr/feed", "feed", 4),
    Source("google_trends_kr", "korea", "https://trends.google.com/trending/rss?geo=KR", "feed", 10),
    Source("wikipedia_ko_top", "korea", "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/ko.wikipedia/all-access/{wiki_day}", "wikipedia", 50),
    Source("appstore_kr_top_free", "korea", "https://rss.marketingtools.apple.com/api/v2/kr/apps/top-free/{limit}/apps.json", "apple", 50),
    Source("applemusic_kr", "korea", "https://rss.marketingtools.apple.com/api/v2/kr/music/most-played/{limit}/songs.json", "apple", 50),
    Source("reddit_korea", "korea", "https://arctic-shift.photon-reddit.com/api/posts/search?limit={limit}&sort=desc&subreddit=korea", "reddit", 100),
)


def source_url(source, now):
    week = now.astimezone(timezone.utc) - timedelta(days=7)
    return source.url.format(limit=source.limit, week_ts=int(week.timestamp()),
                             wiki_day=(now.astimezone(timezone.utc) - timedelta(days=2)).strftime("%Y/%m/%d"))


def clean(value):
    # Decode before stripping, including doubly encoded feed markup.
    text = html.unescape(html.unescape(str(value or "")))
    return " ".join(re.sub(r"<[^>]*>", " ", text).split())


def day(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    try:
        if isinstance(value, (int, float)):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        else:
            value = value.strip()
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                stamp = parsedate_to_datetime(value)
        if stamp.tzinfo:
            stamp = stamp.astimezone(timezone.utc)
        return stamp.date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def row(title, url, date="", summary="", label="", value=None, **extra):
    return {"title": clean(title), "summary": clean(summary)[:200],
            "url": atom_read.without_utm(str(url or "").strip()),
            "date": day(date), "metric": {"label": label, "value": value}, "extra": extra}


def parse_feed(text):
    return [row(e["title"], e["link"], e["published"] or e["updated"],
                " / ".join(e.get("news_titles", [])) or e["content"],
                "approx_traffic" if e.get("approx_traffic") else "", e.get("approx_traffic") or None,
                news_titles=[clean(t) for t in e.get("news_titles", [])])
            for e in atom_read.parse_entries(text, max_body_bytes=MAX_BODY_BYTES)]


def parse_producthunt(text):
    return [row(e["name"], e["url"], e["date"], e["tagline"], author=e["author"])
            for e in producthunt.parse_feed(text)]


def parse_geeknews(text):
    return [row(e["title"], e["url"], e["date"], e["summary"], author=e["author"])
            for e in geeknews.parse_feed(text)]


def parse_hn(text):
    return [row(e.get("title"), e.get("url") or f"https://news.ycombinator.com/item?id={e['objectID']}",
                e.get("created_at"), label="points", value=e.get("points", 0),
                num_comments=e.get("num_comments", 0)) for e in json.loads(text)["hits"]]


def parse_github(text):
    items = []
    for block in re.findall(r'<article\b[^>]*class=["\'][^"\']*\bBox-row\b[^"\']*["\'][^>]*>(.*?)</article>', text, re.S):
        heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", block, re.S)
        repo = re.search(r'href=["\']/([^"\']+)["\']', heading[1]) if heading else None
        if not repo:
            raise ValueError("GitHub trending HTML structure changed: repository heading missing")
        description = re.search(r"<p\b[^>]*>(.*?)</p>", block, re.S)
        language = re.search(r'itemprop=["\']programmingLanguage["\'][^>]*>(.*?)<', block, re.S)
        gained = re.search(r"([\d,]+)\s+stars? (this week|today|this month)", clean(block))
        items.append(row(repo[1], "https://github.com/" + repo[1],
                         summary=description[1] if description else "",
                         label="stars " + gained[2] if gained else "stars gained",
                         value=int(gained[1].replace(",", "")) if gained else None,
                         language=clean(language[1]) if language else ""))
    if not items:
        raise ValueError("GitHub trending HTML structure changed: no Box-row repositories")
    return items


def _parse_hf(text, spaces=False):
    return [row(e["id"], "https://huggingface.co/" + ("spaces/" if spaces else "") + e["id"],
                e.get("createdAt"), e.get("pipeline_tag") or e.get("sdk"), "likes", e.get("likes", 0),
                pipeline_tag=e.get("pipeline_tag", "")) for e in json.loads(text)]


def parse_hf_models(text):
    return _parse_hf(text)


def parse_hf_spaces(text):
    return _parse_hf(text, spaces=True)


def parse_hf_papers(text):
    return [row(e["paper"]["title"], "https://huggingface.co/papers/" + e["paper"]["id"],
                e.get("publishedAt") or e["paper"].get("publishedAt"), e["paper"].get("summary"),
                "upvotes", e["paper"].get("upvotes", 0)) for e in json.loads(text)]


def parse_lobsters(text):
    return [row(e["title"], e.get("url") or e["short_id_url"], e.get("created_at"),
                ", ".join(e.get("tags", [])), "score", e.get("score", 0),
                num_comments=e.get("comment_count", 0)) for e in json.loads(text)]


def parse_devto(text):
    return [row(e["title"], e["url"], e.get("published_at"), e.get("description"),
                "reactions", e.get("positive_reactions_count", 0),
                num_comments=e.get("comments_count", 0)) for e in json.loads(text)]


_WIKI_NAMESPACES = frozenset({
    "special", "talk", "user", "user talk", "wikipedia", "wikipedia talk", "file", "file talk",
    "mediawiki", "mediawiki talk", "template", "template talk", "help", "help talk", "category",
    "category talk", "portal", "portal talk", "draft", "draft talk", "module", "module talk",
    "특수", "토론", "사용자", "사용자토론", "위키백과", "위키백과토론", "파일", "파일토론",
    "미디어위키", "미디어위키토론", "틀", "틀토론", "도움말", "도움말토론", "분류", "분류토론",
    "포털", "포털토론", "초안", "초안토론", "모듈", "모듈토론",
})


def parse_wikipedia(text):
    items = []
    for listing in json.loads(text)["items"]:
        date = "-".join(str(listing[k]).zfill(2) for k in ("year", "month", "day"))
        for e in listing["articles"]:
            title = e["article"]
            if title in {"Main_Page", "대문", "위키백과:대문"} or (
                ":" in title and title.split(":", 1)[0].replace("_", " ").casefold() in _WIKI_NAMESPACES
            ):
                continue
            items.append(row(title.replace("_", " "),
                             f"https://{listing['project']}.org/wiki/" + quote(title, safe=""),
                             date, label="views", value=e["views"], rank=e["rank"]))
    return sorted(items, key=lambda e: e["extra"]["rank"])


def parse_apple(text):
    return [row(e["name"], e["url"], summary=e.get("artistName"), label="rank", value=rank,
                release_date=e.get("releaseDate", ""))
            for rank, e in enumerate(json.loads(text)["feed"]["results"], 1)]


def _parse_mastodon(text, tags=False):
    items = []
    for e in json.loads(text):
        history = e.get("history") or []
        items.append(row(e["name"] if tags else e["title"], e["url"],
                         e.get("published_at") or (int(history[0]["day"]) if history else ""),
                         e.get("description", ""), "uses (7 days)",
                         sum(int(h["uses"]) for h in history), history=history))
    return items


def parse_mastodon_links(text):
    return _parse_mastodon(text)


def parse_mastodon_tags(text):
    return _parse_mastodon(text, tags=True)


def parse_reddit(text):
    items = [row(e["title"], "https://www.reddit.com" + e["permalink"] if e["permalink"].startswith("/") else e["permalink"],
                e.get("created_utc"), e.get("selftext"), "score", e.get("score", 0),
                num_comments=e.get("num_comments", 0)) for e in json.loads(text)["data"]
             if e.get("selftext") not in ("[removed]", "[deleted]") and not e.get("removed_by_category")]
    # The archive returns newest first; stable sorting preserves that order on ties.
    return sorted(items, key=lambda item: item["metric"]["value"] or 0, reverse=True)


PARSERS = {
    "feed": parse_feed, "producthunt": parse_producthunt, "geeknews": parse_geeknews,
    "hn": parse_hn, "github": parse_github, "hf_models": parse_hf_models,
    "hf_spaces": parse_hf_spaces, "hf_papers": parse_hf_papers, "lobsters": parse_lobsters,
    "devto": parse_devto, "wikipedia": parse_wikipedia, "apple": parse_apple,
    "mastodon_links": parse_mastodon_links, "mastodon_tags": parse_mastodon_tags,
    "reddit": parse_reddit,
}


def parse_source(source, text):
    items = PARSERS[source.format](text)
    # Wikipedia is a top-50 selection; other responses are preserved in full.
    if source.format == "wikipedia":
        items = items[:source.limit]
    return [dict(item, source=source.id, group=source.group) for item in items if item["title"]]
