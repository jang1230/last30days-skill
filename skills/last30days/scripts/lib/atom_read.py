"""Small, dependency-free Atom and text helpers for feed consumers."""

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import http

MAX_BODY_BYTES = 2 * 1024 * 1024
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch(url, *, timeout=20, max_body_bytes=MAX_BODY_BYTES) -> tuple[str | None, str | None]:
    """Fetch feed text using the honest client name; reject oversized bodies."""
    try:
        text = http.request("GET", url, timeout=timeout, raw=True,
                            headers={"User-Agent": http.USER_AGENT})
        if not isinstance(text, str):
            return None, "Feed fetch failed: expected a text response"
        if len(text.encode("utf-8")) > max_body_bytes:
            return None, f"Feed body exceeds {max_body_bytes / (1024 * 1024):g} MB"
        return text, None
    except Exception as exc:
        return None, f"Feed fetch failed: {exc}"


def strip_html(text) -> str:
    """Strip tags, decode entities, and collapse whitespace."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", str(text or ""))).split())


def mentions(name: str, text: str) -> bool:
    """Match a normalized name, with ASCII-only boundaries and Korean particles."""
    name = " ".join(name.split())
    if not name:
        return False
    # Case folding the entire text would turn non-ASCII neighbours such as
    # sharp-s into ASCII letters. Apply case insensitivity only to the name.
    pattern = "(?i:" + re.escape(name) + ")"
    if re.fullmatch(r"[A-Za-z0-9]", name[0]):
        pattern = r"(?<![A-Za-z0-9])" + pattern
    if re.fullmatch(r"[A-Za-z0-9]", name[-1]):
        pattern += r"(?![A-Za-z0-9])"
    return re.search(pattern, strip_html(text)) is not None


def utc_date(value: str) -> str | None:
    """Convert an offset-bearing ISO timestamp to its UTC publication day."""
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).date().isoformat()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def without_utm(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def parse_entries(xml_text, *, max_body_bytes=None) -> list[dict]:
    """Parse Atom or RSS 2.0, refusing oversized XML and declarations."""
    if max_body_bytes is not None and len(xml_text.encode("utf-8")) > max_body_bytes:
        raise ValueError(f"Feed body exceeds {max_body_bytes / (1024 * 1024):g} MB")
    if "\x00" in xml_text:
        raise ValueError("NUL bytes are not allowed in XML text")
    if re.search(r"<!DOCTYPE|<!ENTITY", xml_text, re.IGNORECASE):
        raise ValueError("DOCTYPE and ENTITY declarations are not allowed")
    root = ET.fromstring(xml_text)
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ValueError("Expected an RSS channel")
        entries = []
        for item in channel.findall("item"):
            news = [node.text or "" for node in item.iter()
                    if node.tag.rsplit("}", 1)[-1] == "news_item_title"]
            traffic = next((node.text or "" for node in item
                            if node.tag.rsplit("}", 1)[-1] == "approx_traffic"), "")
            entries.append({
                "id": item.findtext("guid", default="").strip(),
                "title": item.findtext("title", default="").strip(),
                "link": item.findtext("link", default="").strip(),
                "published": item.findtext("pubDate", default="").strip(),
                "updated": "",
                "content": item.findtext("description", default="").strip(),
                "author": item.findtext("author", default="").strip(),
                "approx_traffic": traffic, "news_titles": news,
            })
        return entries
    if root.tag != ATOM + "feed":
        raise ValueError("Expected an Atom feed")
    entries = []
    for entry in root.findall(ATOM + "entry"):
        values = {key: entry.findtext(ATOM + key, default="").strip()
                  for key in ("id", "title", "published", "updated", "content")}
        values["link"] = next((link.get("href", "") for link in entry.findall(ATOM + "link")
                               if link.get("rel", "alternate") == "alternate"), "")
        values["author"] = entry.findtext(f"{ATOM}author/{ATOM}name", default="").strip()
        entries.append(values)
    return entries
