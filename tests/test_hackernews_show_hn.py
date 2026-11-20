"""Offline tests of the independent Show HN listing."""

from urllib.parse import parse_qs, urlsplit
from unittest import mock

import pytest

from lib import hackernews, http


def test_show_hn_parameters_sort_and_parse():
    response = {"hits": [
        {"objectID": "1", "title": "Show HN: First", "points": 55, "num_comments": 3, "created_at_i": 1794873600},
        {"objectID": "2", "title": "Show HN: Second", "points": 120, "num_comments": 9, "created_at_i": 1794873600},
        {"objectID": "3", "title": "Show HN: Below threshold", "points": 51},
        {"objectID": "4", "title": "Show HN: At threshold", "points": 52},
        {"objectID": "5", "title": "Show HN: No points", "points": None},
    ]}
    with mock.patch.object(http, "request", return_value=response) as request:
        result = hackernews.fetch_show_hn("2026-11-16", "2026-11-20", min_points=52, limit=17)
    request.assert_called_once()
    assert request.call_args.args[0] == "GET"
    query = parse_qs(urlsplit(request.call_args.args[1]).query)
    assert "points" not in request.call_args.args[1]
    assert hackernews.SHOW_HN_OVERFETCH == 100
    assert query == {"tags": ["show_hn"], "hitsPerPage": [str(hackernews.SHOW_HN_OVERFETCH)], "numericFilters": [
        f"created_at_i>{hackernews._date_to_unix('2026-11-16')},"
        f"created_at_i<{hackernews._date_to_unix('2026-11-20') + 86400}"
    ]}
    assert request.call_args.kwargs["headers"]["User-Agent"] == http.USER_AGENT
    assert [item["id"] for item in result["items"]] == ["2", "1", "4"]
    assert result["items"][0]["engagement"] == {"points": 120, "comments": 9}
    assert result["errors"] == []


@pytest.mark.parametrize("limit,expected", [(0, []), (1, ["2"]), (2, ["2", "4"]), (30, ["2", "4", "1"])])
def test_show_hn_local_limit_after_sorting(limit, expected):
    response = {"hits": [
        {"objectID": str(number), "title": "Show HN: Product", "points": points}
        for number, points in [(1, 50), (2, 120), (3, 49), (4, 90)]
    ]}
    with mock.patch.object(http, "request", return_value=response):
        result = hackernews.fetch_show_hn("2026-11-16", "2026-11-20", limit=limit)
    assert [item["id"] for item in result["items"]] == expected
    assert result["errors"] == []


@pytest.mark.parametrize("response", [None, {"hits": [None]}])
def test_show_hn_parse_failure(response):
    with mock.patch.object(http, "request", return_value=response):
        result = hackernews.fetch_show_hn("2026-11-16", "2026-11-20")
    assert result["items"] == [] and result["errors"]


def test_show_hn_request_failure():
    with mock.patch.object(http, "request", side_effect=OSError("offline")) as request:
        assert hackernews.fetch_show_hn("2026-11-16", "2026-11-20") == {"items": [], "errors": ["offline"]}
    request.assert_called_once()
