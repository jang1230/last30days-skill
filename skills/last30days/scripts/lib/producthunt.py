"""Product Hunt's public Atom feed (no vote counts)."""

import re

from . import atom_read

FEED_URL = "https://www.producthunt.com/feed"


def parse_feed(xml_text) -> list[dict]:
    items = []
    for entry in atom_read.parse_entries(xml_text):
        post_id = re.fullmatch(r"tag:www\.producthunt\.com,2005:Post/(\d+)", entry["id"])
        name = atom_read.strip_html(entry["title"])
        if not post_id or not name:
            continue
        paragraph = re.search(r"<p\b[^>]*>(.*?)</p\s*>", entry["content"], re.I | re.S)
        items.append({
            "id": f"ph:{post_id[1]}", "source": "producthunt", "name": name,
            "tagline": atom_read.strip_html(paragraph[1]) if paragraph else "",
            "url": atom_read.without_utm(entry["link"]),
            "date": atom_read.utc_date(entry["published"]), "author": entry["author"],
        })
    return items


def fetch_feed() -> dict:
    try:
        text, error = atom_read.fetch(FEED_URL)
        if error:
            return {"items": [], "errors": [error]}
        return {"items": parse_feed(text), "errors": []}
    except Exception as exc:
        return {"items": [], "errors": [f"Product Hunt feed parse failed: {exc}"]}
