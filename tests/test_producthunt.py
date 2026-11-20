"""Offline Atom feed parsing and honest HTTP identity."""

from unittest import mock

import pytest

from lib import atom_read, http, producthunt

FEED = '''<feed xml:lang="en-US" xmlns="http://www.w3.org/2005/Atom">
<entry><id>tag:www.producthunt.com,2005:Post/1319464</id>
<published>2026-11-16T23:57:39-08:00</published>
<updated>2026-11-19T16:43:00-08:00</updated>
<link rel="self" href="https://example.test/self"/>
<link rel="alternate" href="https://www.producthunt.com/products/tabstack?keep=1&amp;utm_source=feed&amp;utm_medium=rss"/>
<title><![CDATA[Tabstack & Mozilla]]></title>
<content type="html">&lt;p&gt; Give your &lt;b&gt;model&lt;/b&gt; the web &amp;amp; privacy &lt;/p&gt;
&lt;p&gt;&lt;a href="https://example.test"&gt;Discussion&lt;/a&gt;&lt;/p&gt;</content>
<author><name>fmerian</name></author></entry>
<entry><id>tag:www.producthunt.com,2005:Post/2</id></entry>
<entry><title>No id</title></entry>
<entry><id>invalid</id><title>Bad id</title></entry>
</feed>'''


def test_parse_feed():
    assert producthunt.parse_feed(FEED) == [{
        "id": "ph:1319464", "source": "producthunt", "name": "Tabstack & Mozilla",
        "tagline": "Give your model the web & privacy",
        "url": "https://www.producthunt.com/products/tabstack?keep=1",
        "date": "2026-11-17", "author": "fmerian",
    }]
    entry = atom_read.parse_entries(FEED)[0]
    assert entry["updated"] == "2026-11-19T16:43:00-08:00"
    assert entry["published"] == "2026-11-16T23:57:39-08:00"


@pytest.mark.parametrize("declaration", ['<!DOCTYPE feed>', '<!ENTITY bomb "boom">'])
def test_reject_declarations(declaration):
    with pytest.raises(ValueError, match="DOCTYPE|ENTITY"):
        producthunt.parse_feed(declaration + FEED)


@pytest.mark.parametrize("value", ["", "invalid", "2026-11-20", "2026-11-20T12:00:00", None])
def test_bad_date(value):
    assert atom_read.utc_date(value) is None


def test_fetch_honest_identity_and_timeout():
    with mock.patch.object(http, "request", return_value=FEED) as get:
        assert producthunt.fetch_feed()["items"][0]["id"] == "ph:1319464"
    get.assert_called_once_with("GET", producthunt.FEED_URL, timeout=20, raw=True, headers={"User-Agent": http.USER_AGENT})
    assert "Mozilla" not in get.call_args.kwargs["headers"]["User-Agent"]
    with mock.patch.object(http, "request", return_value=FEED) as get:
        assert atom_read.fetch("https://example.test/feed", timeout=7) == (FEED, None)
    assert get.call_args.kwargs["timeout"] == 7


@pytest.mark.parametrize("body", [None, "bad XML", "<!DOCTYPE feed>" + FEED, "한" * 700000])
def test_fetch_errors(body):
    with mock.patch.object(http, "request", return_value=body):
        result = producthunt.fetch_feed()
    assert result["items"] == []
    assert result["errors"]


def test_fetch_oversize_boundary_and_exception():
    with mock.patch.object(http, "request", return_value="x" * atom_read.MAX_BODY_BYTES):
        assert atom_read.fetch("https://example.test/feed")[1] is None
    with mock.patch.object(http, "request", return_value="x" * (atom_read.MAX_BODY_BYTES + 1)):
        assert "2 MB" in atom_read.fetch("https://example.test/feed")[1]
    with mock.patch.object(http, "request", side_effect=OSError("offline")):
        assert "offline" in producthunt.fetch_feed()["errors"][0]


@pytest.mark.parametrize("error,reason", [
    (http.HTTPError("HTTP 403", status_code=403), "HTTP 403"),
    (http.HTTPError("Request timed out"), "Request timed out"),
    (http.HTTPError("DNS resolution failed"), "DNS resolution failed"),
])
def test_fetch_preserves_failure_cause(error, reason):
    with mock.patch.object(http, "request", side_effect=error):
        assert atom_read.fetch(producthunt.FEED_URL) == (None, f"Feed fetch failed: {reason}")
        assert producthunt.fetch_feed() == {"items": [], "errors": [f"Feed fetch failed: {reason}"]}
