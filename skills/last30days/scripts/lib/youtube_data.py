"""Bounded official YouTube API collection for the standalone trend sweep.

Transport is injected from trend_sweep: this module never opens a connection.
Search calls and videos.list units are separate quota counters. Attempts, even
failed ones, consume the local budget; no request is retried or paginated.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus, urlencode

from . import trend_sources

API = "https://www.googleapis.com/youtube/v3/"
DEFAULT_TOPICS = ("AI", "인공지능", "챗GPT", "AI 에이전트", "자동화", "창업",
                  "부업", "쇼츠", "밈", "챌린지", "신상", "리뷰")
PARTS = "snippet,statistics,contentDetails"


def scrub(value, key):
    """Remove a credential, including URL-escaped echoes, from outward data."""
    if isinstance(value, dict):
        return {scrub(k, key): scrub(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str) and key:
        for secret in sorted({key, quote(key, safe=""), quote_plus(key)}, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
    return value


def duration_seconds(value):
    """Parse an ISO-8601 day/time duration; unknown values stay unknown."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match or not any(v is not None for v in match.groups()):
        return None
    return sum(int(v or 0) * scale for v, scale in zip(match.groups(), (86400, 3600, 60, 1)))


def has_hangul(value):
    return bool(re.search(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", value))


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _item(video, source, topic=None, snippet=None):
    snippet = {**(snippet or {}), **video.get("snippet", {})}
    title = trend_sources.clean(snippet.get("title", ""))
    channel = trend_sources.clean(snippet.get("channelTitle", ""))
    stats = video.get("statistics", {})
    seconds = duration_seconds(video.get("contentDetails", {}).get("duration"))
    return dict(trend_sources.row(
        title, "https://www.youtube.com/watch?v=" + quote(video["id"], safe=""),
        snippet.get("publishedAt", ""), snippet.get("description", ""),
        "views", _number(stats.get("viewCount")), channel=channel,
        likes=_number(stats.get("likeCount")), comments=_number(stats.get("commentCount")),
        duration_seconds=seconds, short=seconds <= 60 if seconds is not None else None,
        topic=topic, has_hangul=has_hangul(title + channel)), source=source, group="video")


def _items(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Invalid YouTube API response: expected items list")
    return payload["items"]


def collect(api_key, fetch, *, topics=DEFAULT_TOPICS, searches=15, now=None):
    """Collect sequentially; HTTP 400/401/403 stop all further API calls."""
    if searches < 0:
        raise ValueError("youtube searches must be non-negative")
    now = now or datetime.now(timezone.utc)
    topics = list(dict.fromkeys(topics))
    result = {"schema": 1, "collected_at": now.isoformat(),
              "delete_by": (now.date() + timedelta(days=30)).isoformat(),
              "items": [], "sections": [], "errors": {}, "unchecked_topics": [],
              "quota": {"search_calls": 0, "units": 0}, "quota_stopped": False,
              "stopped": False}

    def request(endpoint, params):
        result["quota"]["search_calls" if endpoint == "search" else "units"] += 1
        try:
            payload = json.loads(fetch(API + endpoint + "?" + urlencode(params),
                                       extra_headers={"x-goog-api-key": api_key}))
            return _items(payload)
        except Exception as exc:
            # The shared transport attaches a bounded error body. Never retain
            # that body: it can contain URLs or credential echoes.
            reason = (str(exc) + " " + str(getattr(exc, "body", ""))).lower()
            code = getattr(exc, "code", None)
            if code in {400, 401, 403}:
                result["stopped"] = True
            if code == 403 and any(
                token in reason for token in ("quota", "limit", "exceeded")
            ):
                result["quota_stopped"] = True
            raise

    def failure(section, exc):
        result["errors"][section] = scrub(f"{type(exc).__name__}: {exc}", api_key)

    chart = "youtube_chart_kr"
    result["sections"].append({"source": chart, "topic": None})
    try:
        videos = request("videos", {"part": PARTS, "chart": "mostPopular", "regionCode": "KR", "maxResults": 50})
        result["items"].extend(_item(v, chart) for v in videos if v.get("id"))
    except Exception as exc:
        failure(chart, exc)

    for topic in topics:
        if result["stopped"] or result["quota"]["search_calls"] >= searches:
            result["unchecked_topics"].append(topic)
            continue
        source = "youtube_search_kr"
        section = source + ": " + topic
        result["sections"].append({"source": source, "topic": topic})
        try:
            hits = request("search", {"part": "snippet", "type": "video", "q": topic,
                "regionCode": "KR", "relevanceLanguage": "ko", "order": "viewCount",
                "publishedAfter": (now.astimezone(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "maxResults": 50})
            by_id = {h["id"]["videoId"]: h.get("snippet", {}) for h in hits[:50]
                     if isinstance(h.get("id"), dict) and h["id"].get("videoId")}
            videos = request("videos", {"part": PARTS, "id": ",".join(by_id)}) if by_id else []
            by_video = {v["id"]: v for v in videos if v.get("id") in by_id}
            # Deleted/private videos may disappear from videos.list. Keep the
            # search evidence with unknown statistics, never fabricated zeros.
            items = [_item(by_video.get(vid, {"id": vid}), source, topic, snippet)
                     for vid, snippet in by_id.items()]
            items = [v for v in items if v["extra"]["has_hangul"]]
            items.sort(key=lambda v: v["metric"]["value"] if v["metric"]["value"] is not None else -1, reverse=True)
            result["items"].extend(items)
        except Exception as exc:
            failure(section, exc)
            result["unchecked_topics"].append(topic)
    return scrub(result, api_key)
