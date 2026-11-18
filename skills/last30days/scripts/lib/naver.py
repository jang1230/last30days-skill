"""Naver search source for last30days (Korean blogs and news).

Uses the Naver Search API, which two platforms serve with the same parameters
and the same response body:

- NAVER API HUB on NAVER Cloud Platform (``NCP_APIGW_API_KEY_ID`` /
  ``NCP_APIGW_API_KEY``): ``GET https://naverapihub.apigw.ntruss.com/search/v1/<kind>``
- Naver Developers (``NAVER_CLIENT_ID`` / ``NAVER_CLIENT_SECRET``):
  ``GET https://openapi.naver.com/v1/search/<kind>.json``

The two key pairs are not interchangeable, so the platform is chosen by which
pair is set, never by looking at the values. A complete API HUB pair wins.

The source is off by default: ``INCLUDE_SOURCES=naver`` or ``--search naver``
turns it on, and only when a complete key pair is configured.

The API has no date-range parameter. Each kind is requested with ``sort=date``
(newest first) and filtered to the window locally, paging until a page reaches
past the window start, within ``MAX_PAGES`` and ``PAGING_BUDGET_SECONDS``.

Blog rows carry ``postdate`` (``YYYYMMDD``) and news rows carry ``pubDate``
(RFC 822, ``+0900``). A row whose date is missing or unreadable is dropped
rather than passed through the window. The API returns no likes or comment
counts, so items carry an empty ``engagement`` and rank on relevance and
recency alone.
"""

from __future__ import annotations

import datetime
import email.utils
import hashlib
import html
import re
import threading
import time
import urllib.parse
from typing import Any, NamedTuple

from . import http, log
from .relevance import token_overlap_relevance

HUB_SEARCH_URL = "https://naverapihub.apigw.ntruss.com/search/v1"
LEGACY_SEARCH_URL = "https://openapi.naver.com/v1/search"
PROVIDER_HUB = "hub"
PROVIDER_LEGACY = "legacy"
PROVIDER_LABELS = {PROVIDER_HUB: "NAVER API HUB", PROVIDER_LEGACY: "Naver Developers"}

KINDS = ("blog", "news")
# The API's own ceilings: display is 1-100 and start is 1-1000, so a query
# reaches at most the 1,000 newest results of a kind.
DISPLAY = 100
MAX_START = 1000
# Pages requested per kind when the window ends today. A window that ends
# before today has to page past everything newer first, so it may use every
# page the API allows.
PAGES = {"quick": 1, "default": 2, "deep": 3}
MAX_PAGES = MAX_START // DISPLAY
# Items kept per kind, so a busy blog query cannot crowd out the news.
KEEP_PER_KIND = {"quick": 10, "default": 20, "deep": 30}
# Hits below this token-overlap score are dropped. Same bar as dev.to search:
# date-sorted results match any query term, so the newest page of a
# multi-word query is mostly noise.
SEARCH_RELEVANCE_FLOOR = 0.25
# Total wall-clock cap per request; the socket timeout alone bounds each read.
REQUEST_DEADLINE_SECONDS = 20.0
# Shared by every page after the first, across both kinds.
PAGING_BUDGET_SECONDS = 30.0

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_POSTDATE_RE = re.compile(r"\d{8}")


class Credentials(NamedTuple):
    provider: str
    client_id: str
    client_secret: str


def _log(msg: str) -> None:
    log.source_log("Naver", msg, tty_only=False)


def resolve_credentials(config: dict[str, Any] | None) -> Credentials | None:
    """Pick the key pair to call with, or None when no pair is complete.

    A complete API HUB pair wins. A half-set API HUB pair falls back to a
    complete Naver Developers pair, so a user who is midway through moving to
    API HUB keeps working.
    """
    config = config or {}

    def value(key: str) -> str:
        return str(config.get(key) or "").strip()

    hub_id, hub_secret = value("NCP_APIGW_API_KEY_ID"), value("NCP_APIGW_API_KEY")
    if hub_id and hub_secret:
        return Credentials(PROVIDER_HUB, hub_id, hub_secret)
    legacy_id, legacy_secret = value("NAVER_CLIENT_ID"), value("NAVER_CLIENT_SECRET")
    if legacy_id and legacy_secret:
        return Credentials(PROVIDER_LEGACY, legacy_id, legacy_secret)
    return None


def is_available(config: dict[str, Any] | None) -> bool:
    return resolve_credentials(config) is not None


def incomplete_pairs(config: dict[str, Any] | None) -> list[str]:
    """Name each key pair that has exactly one of its two values set."""
    config = config or {}
    pairs = (
        ("NCP_APIGW_API_KEY_ID", "NCP_APIGW_API_KEY"),
        ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"),
    )
    notes = []
    for first, second in pairs:
        has_first = bool(str(config.get(first) or "").strip())
        has_second = bool(str(config.get(second) or "").strip())
        if has_first != has_second:
            notes.append(f"{first if has_first else second} is set without {second if has_first else first}")
    return notes


def _endpoint(credentials: Credentials, kind: str) -> str:
    if credentials.provider == PROVIDER_HUB:
        # API HUB answers JSON by default (its ``format`` parameter).
        return f"{HUB_SEARCH_URL}/{kind}"
    return f"{LEGACY_SEARCH_URL}/{kind}.json"


def _headers(credentials: Credentials) -> dict[str, str]:
    if credentials.provider == PROVIDER_HUB:
        return {
            "X-NCP-APIGW-API-KEY-ID": credentials.client_id,
            "X-NCP-APIGW-API-KEY": credentials.client_secret,
        }
    return {
        "X-Naver-Client-Id": credentials.client_id,
        "X-Naver-Client-Secret": credentials.client_secret,
    }


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _is_historical(to_date: str | None) -> bool:
    return bool(to_date) and str(to_date) < _today().isoformat()


# The pipeline calls a source once per subquery and may retry a thin one, so
# identical requests are fetched once per process. The key holds the platform
# and the URL, never a credential.
_RESPONSE_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_URL_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def clear_cache() -> None:
    with _LOCKS_GUARD:
        _RESPONSE_CACHE.clear()
        _URL_LOCKS.clear()


def _get_rows(
    credentials: Credentials, kind: str, params: dict[str, Any], deadline: float | None = None,
) -> list[dict[str, Any]]:
    url = f"{_endpoint(credentials, kind)}?{urllib.parse.urlencode(params)}"
    key = (credentials.provider, url)
    with _LOCKS_GUARD:
        lock = _URL_LOCKS.setdefault(key, threading.Lock())
    with lock:
        if key not in _RESPONSE_CACHE:
            data = http.request(
                "GET", url, headers=_headers(credentials), timeout=15, retries=2,
                deadline_monotonic=min(
                    time.monotonic() + REQUEST_DEADLINE_SECONDS,
                    deadline if deadline is not None else float("inf"),
                ),
                owned_get=True,
            )
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                # A successful search always carries an ``items`` array, empty
                # when nothing matched; anything else is a changed or error
                # response, not "no matches".
                raise ValueError(f"unexpected Naver {kind} response shape: {type(data).__name__}")
            _RESPONSE_CACHE[key] = [row for row in rows if isinstance(row, dict)]
        return [dict(row) for row in _RESPONSE_CACHE[key]]


def strip_markup(text: Any) -> str:
    """Drop the ``<b>`` match highlighting and decode HTML entities."""
    cleaned = html.unescape(_TAG_RE.sub("", str(text or "")))
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def row_date(kind: str, row: dict[str, Any]) -> str | None:
    """Publication day as ``YYYY-MM-DD``, or None when absent or unreadable.

    The day is the one the API states, in its own offset (Korea time), with
    no conversion: a blog ``postdate`` has no time of day to convert.
    """
    if kind == "blog":
        raw = str(row.get("postdate") or "").strip()
        if not _POSTDATE_RE.fullmatch(raw):
            return None
        try:
            return datetime.datetime.strptime(raw, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    raw = str(row.get("pubDate") or "").strip()
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return None


def _row_text(row: dict[str, Any]) -> str:
    return f"{strip_markup(row.get('title'))} {strip_markup(row.get('description'))}"


def _fetch_kind(
    credentials: Credentials,
    kind: str,
    query: str,
    from_date: str | None,
    to_date: str | None,
    *,
    depth: str,
    deadline: float,
) -> list[dict[str, Any]]:
    """Page one kind newest-first until a page reaches past the window start."""
    max_pages = MAX_PAGES if _is_historical(to_date) else PAGES.get(depth, PAGES["default"])
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        start = 1 + (page - 1) * DISPLAY
        if page > 1 and time.monotonic() >= deadline:
            _log(f"{kind} '{query}': stopped after {page - 1} pages at the time budget; older in-window results may be missing")
            return rows
        params = {"query": query, "display": DISPLAY, "start": start, "sort": "date"}
        try:
            batch = _get_rows(credentials, kind, params, deadline=deadline if page > 1 else None)
        except Exception as exc:
            if page == 1:
                raise
            _log(f"{kind} '{query}': page {page} failed ({exc}); keeping the {page - 1} pages fetched")
            return rows
        rows.extend(batch)
        if len(batch) < DISPLAY:
            return rows
        dated = [day for day in (row_date(kind, row) for row in batch) if day]
        if from_date and dated and min(dated) < from_date:
            # Newest first: once a page holds a row older than the window
            # start, every later page is older still.
            return rows
    if rows and len(rows) >= max_pages * DISPLAY:
        _log(f"{kind} '{query}': stopped at {max_pages} pages; older in-window results may be missing")
    return rows


def _failure_detail(credentials: Credentials, exc: Exception) -> str:
    detail = str(exc)
    if getattr(exc, "status_code", None) == 401:
        label = PROVIDER_LABELS[credentials.provider]
        if credentials.provider == PROVIDER_HUB:
            hint = (
                "check that the Search API is enabled for this Application in the "
                "NAVER API HUB console and that the keys are API HUB keys, not "
                "Naver Developers keys"
            )
        else:
            hint = (
                "check that the keys are Naver Developers keys; API HUB keys go in "
                "NCP_APIGW_API_KEY_ID and NCP_APIGW_API_KEY"
            )
        detail = f"{detail} ({label} rejected the keys: {hint})"
    return detail


def search_naver(
    topic: str,
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    depth: str = "default",
    credentials: Credentials | None = None,
    search_query: str | None = None,
) -> dict[str, Any]:
    """Search Naver blogs and news for the topic inside the window.

    Returns ``{"items": [...], "provider": ..., "errors": [...]}``. A failed
    kind is recorded in ``errors`` and the other kind still counts.
    """
    if credentials is None:
        message = (
            "Naver search is not configured: set NCP_APIGW_API_KEY_ID and "
            "NCP_APIGW_API_KEY (NAVER API HUB) or NAVER_CLIENT_ID and "
            "NAVER_CLIENT_SECRET (Naver Developers)"
        )
        return {"items": [], "provider": None, "errors": [message], "error": message}

    # Planner queries quote phrases for engines with phrase syntax; the API
    # documents no phrase operator, so the quotes are dropped.
    query = " ".join((search_query or topic or "").replace('"', " ").split())
    result: dict[str, Any] = {"items": [], "provider": credentials.provider, "errors": []}
    if not query:
        return result

    paging_deadline = time.monotonic() + PAGING_BUDGET_SECONDS
    keep = KEEP_PER_KIND.get(depth, KEEP_PER_KIND["default"])
    seen_urls: set[str] = set()
    for kind in KINDS:
        try:
            rows = _fetch_kind(
                credentials, kind, query, from_date, to_date,
                depth=depth, deadline=paging_deadline,
            )
        except Exception as exc:
            detail = _failure_detail(credentials, exc)
            result["errors"].append(f"{kind}: {detail}")
            _log(f"{kind} search failed for '{query}': {detail}")
            continue
        undated = out_of_window = off_topic = 0
        relevant: list[tuple[float, str, dict[str, Any]]] = []
        for row in rows:
            url = str(row.get("link") or "").strip()
            if not url or url in seen_urls:
                continue
            day = row_date(kind, row)
            if not day:
                undated += 1
                continue
            if (from_date and day < from_date) or (to_date and day > to_date):
                out_of_window += 1
                continue
            score = token_overlap_relevance(topic or query, _row_text(row))
            if score < SEARCH_RELEVANCE_FLOOR:
                off_topic += 1
                continue
            seen_urls.add(url)
            relevant.append((score, day, {**row, "_naver_kind": kind, "_naver_date": day}))
        # Most relevant first, newest first among equals.
        relevant.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        kept = [row for _score, _day, row in relevant[:keep]]
        result["items"].extend(kept)
        _log(
            f"{kind} '{query}': kept {len(kept)} of {len(rows)} "
            f"(out of window {out_of_window}, undated {undated}, off-topic {off_topic})"
        )

    if len(result["errors"]) == len(KINDS):
        # Every kind failed: surface it so the run reports a source failure
        # instead of a quiet empty result.
        result["error"] = "; ".join(result["errors"])
    return result


def _publisher(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def parse_naver_response(response: dict[str, Any], query: str = "") -> list[dict[str, Any]]:
    """Map Naver search rows to engine item dicts."""
    items: list[dict[str, Any]] = []
    for row in response.get("items") or []:
        kind = str(row.get("_naver_kind") or "blog")
        title = strip_markup(row.get("title"))
        text = strip_markup(row.get("description"))
        url = str(row.get("link") or "").strip()
        day = row.get("_naver_date") or row_date(kind, row)
        relevance = token_overlap_relevance(query, f"{title} {text}") if query else 0.5
        metadata: dict[str, Any] = {"kind": kind}
        if kind == "news":
            original_url = str(row.get("originallink") or "").strip()
            author = _publisher(original_url or url)
            if original_url and original_url != url:
                metadata["original_url"] = original_url
        else:
            author = strip_markup(row.get("bloggername"))
            blog_url = str(row.get("bloggerlink") or "").strip()
            if blog_url:
                metadata["blog_url"] = blog_url
        items.append({
            "id": f"naver-{kind}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}",
            "title": title,
            "url": url,
            "author": author,
            "date": day,
            "text": text,
            # The search API returns no likes or comment counts.
            "engagement": {},
            "relevance": round(max(0.0, min(1.0, relevance)), 2),
            "why_relevant": f"Naver {kind} search",
            "metadata": metadata,
        })
    return items
