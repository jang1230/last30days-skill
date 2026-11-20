"""GeekNews's single-quoted Atom, CDATA and Korea-to-UTC dates."""

from unittest import mock

import pytest

from lib import geeknews, http

FEED = '''<feed xmlns='http://www.w3.org/2005/Atom'><entry>
<title><![CDATA[Kiri - 무료 Docker & Desktop 대체재]]></title>
<link rel='alternate' type='text/html' href='https://news.hada.io/topic?id=37498&amp;utm_source=geeknews&amp;utm_medium=rss&amp;utm_campaign=news'/>
<id>https://news.hada.io/topic?id=37498</id>
<updated>2026-11-20T00:37:13+09:00</updated>
<published>2026-11-20T00:37:13+09:00</published>
<author><name>neo</name><uri>https://news.hada.io/user/neo</uri></author>
<content type='html' xml:lang='ko'><![CDATA[<div><ul><li>무료 &amp; 간단한 도구</li></ul></div>]]></content>
</entry><entry><title>No id</title></entry>
<entry><id>https://news.hada.io/topic?id=2</id></entry>
<entry><id>https://news.hada.io/topic?id=nope</id><title>Bad id</title></entry></feed>'''


def test_parse_feed():
    assert geeknews.parse_feed(FEED) == [{
        "id": "gn:37498", "source": "geeknews", "title": "Kiri - 무료 Docker & Desktop 대체재",
        "url": "https://news.hada.io/topic?id=37498", "date": "2026-11-19",
        "summary": "무료 & 간단한 도구", "author": "neo",
    }]
    assert len(geeknews.parse_feed(FEED.replace("무료 &amp; 간단한 도구", "가" * 400))[0]["summary"]) == 300
    assert geeknews.parse_feed(FEED.replace("<published>2026-11-20T00:37:13+09:00</published>", ""))[0]["date"] == "2026-11-19"


@pytest.mark.parametrize("declaration", ['<!DOCTYPE feed>', '<!ENTITY bomb "boom">'])
def test_reject_declarations(declaration):
    with pytest.raises(ValueError, match="DOCTYPE|ENTITY"):
        geeknews.parse_feed(declaration + FEED)


def test_fetch_honest_identity():
    with mock.patch.object(http, "request", return_value=FEED) as get:
        assert geeknews.fetch_feed()["items"][0]["id"] == "gn:37498"
    get.assert_called_once_with("GET", geeknews.FEED_URL, timeout=20, raw=True, headers={"User-Agent": http.USER_AGENT})
    assert "Mozilla" not in get.call_args.kwargs["headers"]["User-Agent"]


@pytest.mark.parametrize("body", [None, "broken XML", "<!ENTITY x 'x'>" + FEED, "x" * (2 * 1024 * 1024 + 1)])
def test_fetch_errors(body):
    with mock.patch.object(http, "request", return_value=body):
        result = geeknews.fetch_feed()
    assert result["items"] == []
    assert result["errors"]


def test_fetch_network_failure():
    with mock.patch.object(http, "request", side_effect=OSError("offline")):
        assert "offline" in geeknews.fetch_feed()["errors"][0]


@pytest.mark.parametrize("reason", ["HTTP 403", "Request timed out", "DNS resolution failed"])
def test_fetch_preserves_failure_cause(reason):
    with mock.patch.object(http, "request", side_effect=http.HTTPError(reason)):
        assert geeknews.fetch_feed() == {"items": [], "errors": [f"Feed fetch failed: {reason}"]}
