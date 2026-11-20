"""GeekNews public Atom feed."""

from urllib.parse import parse_qs, urlsplit

from . import atom_read

FEED_URL = "https://news.hada.io/rss/news"


def parse_feed(xml_text) -> list[dict]:
    items = []
    for entry in atom_read.parse_entries(xml_text):
        topic_id = parse_qs(urlsplit(entry["id"]).query).get("id", [""])[0]
        title = atom_read.strip_html(entry["title"])
        if not topic_id.isascii() or not topic_id.isdigit() or not title:
            continue
        items.append({
            "id": f"gn:{topic_id}", "source": "geeknews", "title": title,
            "url": atom_read.without_utm(entry["link"] or entry["id"]),
            "date": atom_read.utc_date(entry["published"] or entry["updated"]),
            "summary": atom_read.strip_html(entry["content"])[:300],
            "author": entry["author"],
        })
    return items


def fetch_feed() -> dict:
    try:
        text, error = atom_read.fetch(FEED_URL)
        if error:
            return {"items": [], "errors": [error]}
        return {"items": parse_feed(text), "errors": []}
    except Exception as exc:
        return {"items": [], "errors": [f"GeekNews feed parse failed: {exc}"]}
