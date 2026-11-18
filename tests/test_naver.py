"""Naver source: blog and news retrieval, window filtering, Korean relevance."""

import datetime
import urllib.parse
from unittest import mock

import pytest

from lib import cjk, dedupe, env, http, naver, normalize, planner, signals
from lib.relevance import token_overlap_relevance


TODAY = datetime.date(2026, 10, 11)
FROM_DATE = "2026-09-11"
TO_DATE = "2026-10-11"
TOPIC = "갤럭시 폴드 후기"

# Obvious dummies: no real key has this shape.
HUB = naver.Credentials(naver.PROVIDER_HUB, "fake-hub-id", "fake-hub-secret")
LEGACY = naver.Credentials(naver.PROVIDER_LEGACY, "fake-legacy-id", "fake-legacy-secret")


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(naver, "_today", lambda: TODAY)


@pytest.fixture(autouse=True)
def _fresh_cache():
    naver.clear_cache()
    yield
    naver.clear_cache()


@pytest.fixture(autouse=True)
def _bigram_segmentation(monkeypatch):
    # Pin the dictionary-free path so scores do not depend on whether jieba
    # happens to be installed where the tests run.
    monkeypatch.setattr(cjk, "_jieba", None)


def _blog(n, title="<b>갤럭시</b> <b>폴드</b> 한 달 사용 <b>후기</b>", *, postdate="20261005"):
    return {
        "title": title,
        "link": f"https://blog.naver.com/someone/{n}",
        "description": f"<b>갤럭시</b> <b>폴드</b>를 써 본 솔직한 <b>후기</b> {n}번째 &amp; 장단점 정리",
        "bloggername": "어느 블로거의 &quot;일상&quot;",
        "bloggerlink": "blog.naver.com/someone",
        "postdate": postdate,
    }


def _news(n, title="삼성 <b>갤럭시</b> <b>폴드</b> 신제품 <b>후기</b> 쏟아져", *, pub_date="Mon, 05 Oct 2026 09:30:00 +0900"):
    return {
        "title": title,
        "originallink": f"https://www.example-press.co.kr/news/{n}",
        "link": f"https://n.news.naver.com/mnews/article/001/{n}",
        "description": f"<b>갤럭시</b> <b>폴드</b> 사용자 <b>후기</b>가 이어지고 있다. 기사 {n}",
        "pubDate": pub_date,
    }


class FakeApi:
    """Stands in for http.request; pages are keyed by (kind, start)."""

    def __init__(self, blog=None, news=None, fail=()):
        self.pages = {"blog": blog or {}, "news": news or {}}
        self.fail = dict.fromkeys(fail, OSError("down")) if not isinstance(fail, dict) else fail
        self.calls = []

    def __call__(self, method, url, headers=None, **kwargs):
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        kind = parsed.path.rsplit("/", 1)[-1].removesuffix(".json")
        self.calls.append({"url": url, "kind": kind, "query": query, "headers": dict(headers or {}), "kwargs": kwargs})
        assert method == "GET"
        assert query["sort"] == "date"
        if kind in self.fail:
            raise self.fail[kind]
        rows = self.pages[kind].get(int(query["start"]), [])
        return {"lastBuildDate": "Sun, 11 Oct 2026 10:00:00 +0900", "total": len(rows), "start": int(query["start"]), "display": len(rows), "items": rows}

    def starts(self, kind):
        return [int(call["query"]["start"]) for call in self.calls if call["kind"] == kind]


def _install(monkeypatch, api):
    monkeypatch.setattr(naver.http, "request", api)
    return api


def _search(depth="default", credentials=HUB, to_date=TO_DATE, **kwargs):
    return naver.search_naver(TOPIC, FROM_DATE, to_date, depth=depth, credentials=credentials, **kwargs)


# --- credentials ----------------------------------------------------------

def test_no_credentials_means_the_source_is_unavailable():
    assert naver.resolve_credentials({}) is None
    assert naver.resolve_credentials(None) is None
    assert naver.is_available({}) is False


def test_a_complete_api_hub_pair_wins_over_a_developers_pair():
    config = {
        "NCP_APIGW_API_KEY_ID": "fake-hub-id",
        "NCP_APIGW_API_KEY": "fake-hub-secret",
        "NAVER_CLIENT_ID": "fake-legacy-id",
        "NAVER_CLIENT_SECRET": "fake-legacy-secret",
    }
    assert naver.resolve_credentials(config) == HUB
    assert naver.is_available(config) is True


def test_a_developers_pair_alone_is_used():
    config = {"NAVER_CLIENT_ID": " fake-legacy-id ", "NAVER_CLIENT_SECRET": "fake-legacy-secret"}
    assert naver.resolve_credentials(config) == LEGACY


def test_a_half_set_pair_is_not_a_credential():
    assert naver.is_available({"NCP_APIGW_API_KEY_ID": "fake-hub-id"}) is False
    assert naver.is_available({"NAVER_CLIENT_SECRET": "fake-legacy-secret"}) is False
    assert naver.incomplete_pairs({"NCP_APIGW_API_KEY_ID": "fake-hub-id"}) == [
        "NCP_APIGW_API_KEY_ID is set without NCP_APIGW_API_KEY"
    ]
    assert naver.incomplete_pairs({"NAVER_CLIENT_SECRET": "fake-legacy-secret"}) == [
        "NAVER_CLIENT_SECRET is set without NAVER_CLIENT_ID"
    ]
    assert naver.incomplete_pairs({}) == []


def test_a_half_set_api_hub_pair_falls_back_to_a_complete_developers_pair():
    config = {
        "NCP_APIGW_API_KEY_ID": "fake-hub-id",
        "NAVER_CLIENT_ID": "fake-legacy-id",
        "NAVER_CLIENT_SECRET": "fake-legacy-secret",
    }
    assert naver.resolve_credentials(config) == LEGACY


NAVER_CONFIG_KEYS = ("NCP_APIGW_API_KEY_ID", "NCP_APIGW_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")


def _load_config(tmp_path, monkeypatch, env_text=""):
    config_file = tmp_path / ".env"
    config_file.write_text(env_text, encoding="utf-8")
    config_file.chmod(0o600)
    monkeypatch.setenv("LAST30DAYS_CONFIG_DIR", str(tmp_path))
    for key in NAVER_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(env, "CONFIG_FILE", config_file)
    monkeypatch.chdir(tmp_path)
    with mock.patch.object(env, "_load_keychain", return_value={}), mock.patch.object(env, "_load_pass", return_value={}):
        return env.get_config()


def test_naver_keys_load_from_the_env_file(tmp_path, monkeypatch):
    config = _load_config(
        tmp_path, monkeypatch,
        "NCP_APIGW_API_KEY_ID=fake-hub-id\nNCP_APIGW_API_KEY=fake-hub-secret\n",
    )
    assert naver.resolve_credentials(config) == HUB


def test_naver_keys_are_registered_but_unset_by_default(tmp_path, monkeypatch):
    config = _load_config(tmp_path, monkeypatch)
    for key in NAVER_CONFIG_KEYS:
        assert key in config and config[key] is None
    assert naver.is_available(config) is False


def test_naver_keys_add_no_keychain_or_pass_lookups():
    # Every KEYCHAIN_KEYS entry is one more lookup per run for each Keychain
    # or pass user, including on runs that never ask for Naver.
    assert not set(NAVER_CONFIG_KEYS) & set(env.KEYCHAIN_KEYS)


def test_both_key_values_are_registered_for_fixture_redaction(monkeypatch):
    registered = []
    monkeypatch.setattr(naver.http, "add_fixture_redactions", lambda values: registered.extend(values))
    _install(monkeypatch, FakeApi())
    _search(credentials=HUB)
    assert registered == ["fake-hub-id", "fake-hub-secret"]


def test_search_without_credentials_reports_an_error_and_makes_no_request(monkeypatch):
    api = _install(monkeypatch, FakeApi())
    result = naver.search_naver(TOPIC, FROM_DATE, TO_DATE, credentials=None)
    assert result["items"] == []
    assert "not configured" in result["error"]
    assert api.calls == []


# --- request shape --------------------------------------------------------

def test_api_hub_requests_use_the_hub_host_and_headers(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(1)]}))
    result = _search(credentials=HUB)
    assert result["provider"] == "hub"
    assert [call["url"].split("?")[0] for call in api.calls] == [
        "https://naverapihub.apigw.ntruss.com/search/v1/blog",
        "https://naverapihub.apigw.ntruss.com/search/v1/news",
    ]
    for call in api.calls:
        assert call["headers"] == {
            "X-NCP-APIGW-API-KEY-ID": "fake-hub-id",
            "X-NCP-APIGW-API-KEY": "fake-hub-secret",
        }
        assert call["query"] == {"query": TOPIC, "display": "100", "start": "1", "sort": "date"}
        assert "fake-hub" not in call["url"]


def test_developers_requests_use_the_openapi_host_and_headers(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(1)]}))
    result = _search(credentials=LEGACY)
    assert result["provider"] == "legacy"
    assert [call["url"].split("?")[0] for call in api.calls] == [
        "https://openapi.naver.com/v1/search/blog.json",
        "https://openapi.naver.com/v1/search/news.json",
    ]
    for call in api.calls:
        assert call["headers"] == {
            "X-Naver-Client-Id": "fake-legacy-id",
            "X-Naver-Client-Secret": "fake-legacy-secret",
        }


def test_requests_carry_a_total_deadline(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}))
    _search()
    for call in api.calls:
        assert call["kwargs"]["owned_get"] is True
        assert call["kwargs"]["deadline_monotonic"] is not None


def test_planner_quotes_are_stripped_from_the_search_query(monkeypatch):
    api = _install(monkeypatch, FakeApi())
    _search(search_query='"갤럭시 폴드"   후기')
    assert {call["query"]["query"] for call in api.calls} == {"갤럭시 폴드 후기"}


def test_an_empty_query_makes_no_request(monkeypatch):
    api = _install(monkeypatch, FakeApi())
    result = naver.search_naver("", FROM_DATE, TO_DATE, credentials=HUB)
    assert result == {"items": [], "provider": "hub", "errors": []}
    assert api.calls == []


# --- parsing --------------------------------------------------------------

def test_strip_markup_removes_highlight_tags_and_decodes_entities():
    assert naver.strip_markup("<b>갤럭시</b> 폴드 &amp; 플립 &quot;비교&quot;") == '갤럭시 폴드 & 플립 "비교"'
    assert naver.strip_markup("  줄바꿈\n  과   공백  ") == "줄바꿈 과 공백"
    assert naver.strip_markup(None) == ""


def test_row_date_reads_each_kinds_own_field():
    assert naver.row_date("blog", {"postdate": "20261005"}) == "2026-10-05"
    assert naver.row_date("news", {"pubDate": "Mon, 05 Oct 2026 09:30:00 +0900"}) == "2026-10-05"
    # The stated day is kept as written (Korea time), not shifted to UTC.
    assert naver.row_date("news", {"pubDate": "Mon, 05 Oct 2026 00:10:00 +0900"}) == "2026-10-05"


@pytest.mark.parametrize("kind, row", [
    ("blog", {}),
    ("blog", {"postdate": ""}),
    ("blog", {"postdate": "2026-10-05"}),
    ("blog", {"postdate": "20261345"}),
    ("news", {}),
    ("news", {"pubDate": "어제"}),
    # A blog row never carries pubDate and a news row never carries postdate.
    ("news", {"postdate": "20261005"}),
])
def test_row_date_is_none_when_the_date_is_missing_or_unreadable(kind, row):
    assert naver.row_date(kind, row) is None


def test_blog_and_news_rows_parse_into_items(monkeypatch):
    _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(7)]}))
    items = naver.parse_naver_response(_search(), query=TOPIC)
    assert [item["metadata"]["kind"] for item in items] == ["blog", "news"]

    blog, news = items
    assert blog["title"] == "갤럭시 폴드 한 달 사용 후기"
    assert blog["text"] == "갤럭시 폴드를 써 본 솔직한 후기 1번째 & 장단점 정리"
    assert blog["url"] == "https://blog.naver.com/someone/1"
    assert blog["author"] == '어느 블로거의 "일상"'
    assert blog["date"] == "2026-10-05"
    assert blog["metadata"] == {"kind": "blog", "blog_url": "blog.naver.com/someone"}
    assert blog["why_relevant"] == "Naver blog search"

    assert news["title"] == "삼성 갤럭시 폴드 신제품 후기 쏟아져"
    assert news["url"] == "https://n.news.naver.com/mnews/article/001/7"
    assert news["author"] == "example-press.co.kr"
    assert news["date"] == "2026-10-05"
    assert news["metadata"] == {
        "kind": "news",
        "original_url": "https://www.example-press.co.kr/news/7",
    }

    for item in items:
        assert "<b>" not in item["title"] + item["text"]
        assert item["id"].startswith(f"naver-{item['metadata']['kind']}-")
        # The search API returns no reaction counts; none are invented.
        assert item["engagement"] == {}
    assert blog["id"] != news["id"]


def test_news_without_a_separate_original_url_keeps_metadata_small():
    row = {**_news(3), "originallink": "https://n.news.naver.com/mnews/article/001/3",
           "_naver_kind": "news", "_naver_date": "2026-10-05"}
    (item,) = naver.parse_naver_response({"items": [row]}, query=TOPIC)
    assert item["metadata"] == {"kind": "news"}
    assert item["author"] == "n.news.naver.com"


def test_parse_without_a_query_uses_a_neutral_relevance():
    row = {**_blog(1), "_naver_kind": "blog", "_naver_date": "2026-10-05"}
    (item,) = naver.parse_naver_response({"items": [row]})
    assert item["relevance"] == 0.5


# --- window ---------------------------------------------------------------

def test_rows_outside_the_window_or_without_a_date_are_dropped(monkeypatch):
    blog = [
        _blog(1, postdate="20261011"),   # window end
        _blog(2, postdate="20260911"),   # window start
        _blog(3, postdate="20260910"),   # one day too old
        _blog(4, postdate="20261012"),   # after the window
        _blog(5, postdate=""),           # no date
        _blog(6, postdate="not-a-date"),
    ]
    news = [
        _news(1, pub_date="Sun, 11 Oct 2026 08:00:00 +0900"),
        _news(2, pub_date="Thu, 10 Sep 2026 23:59:00 +0900"),  # one day too old
        _news(3, pub_date=""),
    ]
    _install(monkeypatch, FakeApi(blog={1: blog}, news={1: news}))
    items = naver.parse_naver_response(_search(), query=TOPIC)
    assert sorted(item["url"].rsplit("/", 1)[-1] for item in items if item["metadata"]["kind"] == "blog") == ["1", "2"]
    assert [item["url"].rsplit("/", 1)[-1] for item in items if item["metadata"]["kind"] == "news"] == ["1"]
    assert all(item["date"] for item in items)


def test_a_past_window_keeps_its_upper_bound(monkeypatch):
    blog = [_blog(1, postdate="20261010"), _blog(2, postdate="20260925"), _blog(3, postdate="20260901")]
    _install(monkeypatch, FakeApi(blog={1: blog}))
    result = naver.search_naver(TOPIC, "2026-09-15", "2026-09-30", credentials=HUB)
    assert [row["postdate"] for row in result["items"]] == ["20260925"]


# --- paging ---------------------------------------------------------------

def _full_page(first, postdate):
    return [_blog(first + offset, postdate=postdate) for offset in range(naver.DISPLAY)]


def test_paging_stops_once_a_page_reaches_past_the_window_start(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={
        1: _full_page(0, "20261010"),
        101: _full_page(100, "20260901"),   # older than the window start
        201: _full_page(200, "20260801"),
    }))
    _search(depth="deep")
    assert api.starts("blog") == [1, 101]


def test_a_short_page_ends_paging(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)], 101: _full_page(100, "20261001")}))
    _search(depth="deep")
    assert api.starts("blog") == [1]


@pytest.mark.parametrize("depth, starts", [
    ("quick", [1]),
    ("default", [1, 101]),
    ("deep", [1, 101, 201]),
    ("unknown-depth", [1, 101]),
])
def test_a_current_window_pages_by_depth(monkeypatch, depth, starts):
    pages = {start: _full_page(start, "20261010") for start in range(1, 1001, 100)}
    api = _install(monkeypatch, FakeApi(blog=pages))
    _search(depth=depth)
    assert api.starts("blog") == starts


def test_a_past_window_pages_to_the_api_ceiling(monkeypatch):
    # Everything is newer than the window, so only the 1,000-result ceiling
    # (start <= 1000) ends the paging.
    pages = {start: _full_page(start, "20261010") for start in range(1, 2001, 100)}
    api = _install(monkeypatch, FakeApi(blog=pages))
    result = naver.search_naver(TOPIC, "2026-08-01", "2026-08-31", depth="quick", credentials=HUB)
    assert api.starts("blog") == list(range(1, 1001, 100))
    assert result["items"] == []


def test_paging_stops_at_the_time_budget(monkeypatch):
    monkeypatch.setattr(naver, "PAGING_BUDGET_SECONDS", 0.0)
    pages = {start: _full_page(start, "20261010") for start in (1, 101, 201)}
    api = _install(monkeypatch, FakeApi(blog=pages, news=pages))
    result = _search(depth="deep")
    # The first page of every kind is always fetched; later pages need budget.
    assert api.starts("blog") == [1]
    assert api.starts("news") == [1]
    assert result["errors"] == []


def test_a_later_page_failure_keeps_the_pages_already_fetched(monkeypatch):
    class FailsOnPageTwo(FakeApi):
        def __call__(self, method, url, headers=None, **kwargs):
            if "start=101" in url:
                self.calls.append({"url": url, "kind": "blog", "query": {"start": "101"}, "headers": {}, "kwargs": kwargs})
                raise OSError("page two down")
            return super().__call__(method, url, headers=headers, **kwargs)

    _install(monkeypatch, FailsOnPageTwo(blog={1: _full_page(0, "20261010")}))
    result = _search()
    assert result["errors"] == []
    assert len(result["items"]) == naver.KEEP_PER_KIND["default"]


@pytest.mark.parametrize("depth", ["quick", "default", "deep"])
def test_each_kind_is_capped_so_blogs_cannot_crowd_out_news(monkeypatch, depth):
    news = [_news(n) for n in range(40)]
    _install(monkeypatch, FakeApi(blog={1: _full_page(0, "20261010")}, news={1: news}))
    result = _search(depth=depth)
    kinds = [row["_naver_kind"] for row in result["items"]]
    assert kinds.count("blog") == naver.KEEP_PER_KIND[depth]
    assert kinds.count("news") == naver.KEEP_PER_KIND[depth]


def test_identical_requests_are_fetched_once(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(1)]}))
    first = _search()
    second = _search()
    assert len(api.calls) == 2
    assert first["items"] == second["items"]


def test_a_duplicate_link_is_kept_once(monkeypatch):
    _install(monkeypatch, FakeApi(blog={1: [_blog(1), _blog(1), {**_blog(2), "link": ""}]}))
    assert len(_search()["items"]) == 1


# --- failures -------------------------------------------------------------

@pytest.mark.parametrize("failing, surviving", [("blog", "news"), ("news", "blog")])
def test_one_kind_failing_does_not_block_the_other(monkeypatch, failing, surviving):
    _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(1)]}, fail=(failing,)))
    result = _search()
    assert [row["_naver_kind"] for row in result["items"]] == [surviving]
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith(f"{failing}: ")
    # A partial result is not a source failure.
    assert "error" not in result


def test_every_kind_failing_reports_a_source_error(monkeypatch):
    _install(monkeypatch, FakeApi(fail=("blog", "news")))
    result = _search()
    assert result["items"] == []
    assert result["error"] == "blog: down; news: down"


def test_an_unexpected_response_shape_is_an_error_not_an_empty_result(monkeypatch):
    monkeypatch.setattr(naver.http, "request", lambda *args, **kwargs: {"errorCode": "SE01", "errorMessage": "bad"})
    result = _search()
    assert result["items"] == []
    assert "unexpected Naver blog response shape" in result["error"]


@pytest.mark.parametrize("credentials, expected", [
    (HUB, "enabled for this Application"),
    (LEGACY, "API HUB keys go in NCP_APIGW_API_KEY_ID"),
])
def test_a_rejected_key_says_which_platform_rejected_it(monkeypatch, credentials, expected):
    rejected = http.HTTPError("HTTP 401: Unauthorized", status_code=401)
    _install(monkeypatch, FakeApi(fail={"blog": rejected, "news": rejected}))
    result = _search(credentials=credentials)
    assert expected in result["error"]
    assert naver.PROVIDER_LABELS[credentials.provider] in result["error"]
    assert credentials.client_id not in result["error"]
    assert credentials.client_secret not in result["error"]


# --- Korean relevance and de-duplication ----------------------------------

def test_a_korean_topic_scores_against_korean_text():
    assert token_overlap_relevance(TOPIC, "갤럭시 폴드를 한 달 써 본 솔직한 후기입니다") > 0.5
    # Particles attached to the noun do not hide the match.
    assert token_overlap_relevance("갤럭시 폴드", "갤럭시폴드가 드디어 출시됐습니다") > 0.25
    assert token_overlap_relevance(TOPIC, "오늘 점심은 김치찌개를 먹었습니다") == 0.0


def test_korean_items_carry_a_nonzero_relevance(monkeypatch):
    _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(1)]}))
    items = naver.parse_naver_response(_search(), query=TOPIC)
    assert len(items) == 2
    assert all(item["relevance"] > 0.25 for item in items)


def test_off_topic_korean_rows_are_dropped(monkeypatch):
    off_topic = {**_blog(9), "title": "오늘 점심 김치찌개 맛집", "description": "회사 근처 김치찌개 집을 다녀왔습니다"}
    _install(monkeypatch, FakeApi(blog={1: [_blog(1), off_topic]}))
    result = _search()
    assert [row["link"] for row in result["items"]] == ["https://blog.naver.com/someone/1"]


def test_the_most_relevant_rows_are_kept_first(monkeypatch):
    weak = [_blog(n, title="<b>후기</b> 모음", postdate="20261010") for n in range(naver.DISPLAY - 1)]
    for row in weak:
        row["description"] = "여러 제품 <b>후기</b>를 모았습니다 갤럭시"
    strong = _blog(999, postdate="20260920")
    _install(monkeypatch, FakeApi(blog={1: [*weak, strong]}))
    result = _search(depth="quick")
    assert result["items"][0]["link"] == "https://blog.naver.com/someone/999"


def test_near_identical_korean_titles_are_seen_as_duplicates():
    original = "삼성 갤럭시 폴드 신제품 후기 쏟아져"
    reprint = "삼성 갤럭시 폴드 신제품 후기 쏟아져 (종합)"
    unrelated = "오늘 점심은 회사 근처 김치찌개 맛집에서"
    assert dedupe.token_jaccard(original, reprint) > 0.7
    assert dedupe.hybrid_similarity(original, reprint) > 0.7
    assert dedupe.token_jaccard(original, unrelated) == 0.0


# --- normalization ---------------------------------------------------------

def _normalized(monkeypatch, blog=None, news=None):
    _install(monkeypatch, FakeApi(blog={1: blog or []}, news={1: news or []}))
    raw = naver.parse_naver_response(_search(), query=TOPIC)
    return normalize.normalize_source_items("naver", raw, FROM_DATE, TO_DATE)


def test_normalized_items_keep_text_dates_and_empty_engagement(monkeypatch):
    blog, news = _normalized(monkeypatch, blog=[_blog(1)], news=[_news(7)])

    assert blog.source == "naver"
    assert blog.title == "갤럭시 폴드 한 달 사용 후기"
    assert blog.body == "갤럭시 폴드 한 달 사용 후기\n갤럭시 폴드를 써 본 솔직한 후기 1번째 & 장단점 정리"
    assert blog.snippet == "갤럭시 폴드를 써 본 솔직한 후기 1번째 & 장단점 정리"
    assert blog.author == '어느 블로거의 "일상"'
    assert blog.container == "Naver Blog"
    assert blog.url == "https://blog.naver.com/someone/1"

    assert news.container == "Naver News"
    assert news.author == "example-press.co.kr"
    assert news.metadata["original_url"] == "https://www.example-press.co.kr/news/7"

    for item in (blog, news):
        assert item.published_at == "2026-10-05"
        assert item.date_confidence == "high"
        assert item.engagement == {}
        assert item.relevance_hint > 0.25


def test_an_undated_item_is_low_confidence_not_trusted():
    raw = [{"id": "naver-blog-x", "title": "갤럭시 폴드 후기", "url": "https://blog.naver.com/someone/9",
            "text": "본문", "date": None, "metadata": {"kind": "blog"}}]
    (item,) = normalize.normalize_source_items("naver", raw, FROM_DATE, TO_DATE)
    assert item.published_at is None
    assert item.date_confidence == "low"


def test_the_normalizer_does_not_pass_out_of_window_items():
    raw = [
        {"id": "a", "title": "갤럭시 폴드 후기", "url": "https://blog.naver.com/someone/1", "date": "2026-10-05", "metadata": {"kind": "blog"}},
        {"id": "b", "title": "갤럭시 폴드 후기", "url": "https://blog.naver.com/someone/2", "date": "2026-08-01", "metadata": {"kind": "blog"}},
    ]
    items = normalize.normalize_source_items("naver", raw, FROM_DATE, TO_DATE)
    assert [item.item_id for item in items] == ["a"]


def test_an_item_without_a_known_kind_still_normalizes():
    raw = [{"title": "", "url": "https://example.com/x", "date": "2026-10-05"}]
    (item,) = normalize.normalize_source_items("naver", raw, FROM_DATE, TO_DATE)
    assert item.item_id == "NV1"
    assert item.title == "Naver result 1"
    assert item.container == "Naver"


# --- scoring ---------------------------------------------------------------

def test_naver_items_get_no_engagement_score(monkeypatch):
    items = _normalized(monkeypatch, blog=[_blog(1)], news=[_news(7)])
    assert "naver" in signals._NO_ENGAGEMENT_SCORE_SOURCES
    for item in items:
        assert signals.engagement_raw(item) is None
        assert signals.engagement_score(item) is None


def _ranked(monkeypatch, blog):
    items = _normalized(monkeypatch, blog=blog)
    ranked = signals.annotate_stream(items, TOPIC, "balanced_recent", reference_date=TO_DATE)
    for item in ranked:
        assert item.engagement_score is None
        assert item.source_quality == signals.SOURCE_QUALITY["naver"]
    return ranked


def test_with_equal_text_the_newer_naver_item_ranks_first(monkeypatch):
    ranked = _ranked(monkeypatch, [_blog(2, postdate="20260912"), _blog(1, postdate="20261010")])
    assert [item.url.rsplit("/", 1)[-1] for item in ranked] == ["1", "2"]
    assert ranked[0].local_relevance == ranked[1].local_relevance > 0
    assert ranked[0].freshness > ranked[1].freshness


def test_on_the_same_day_the_more_relevant_naver_item_ranks_first(monkeypatch):
    weaker = {**_blog(2), "title": "<b>후기</b> 모음", "description": "<b>갤럭시</b> 외 여러 제품"}
    ranked = _ranked(monkeypatch, [weaker, _blog(1)])
    assert [item.url.rsplit("/", 1)[-1] for item in ranked] == ["1", "2"]
    assert ranked[0].freshness == ranked[1].freshness
    assert ranked[0].local_relevance > ranked[1].local_relevance > 0


# --- planning --------------------------------------------------------------

def test_the_planner_knows_what_naver_offers():
    assert planner.SOURCE_CAPABILITIES["naver"] == {"discussion", "link", "reference"}


@pytest.mark.parametrize("intent", ["how_to", "comparison", "opinion", "breaking_news"])
def test_an_available_naver_is_a_default_source_for_every_intent(intent):
    sources = planner._default_sources_for_intent(intent, ["reddit", "youtube", "naver"])
    assert "naver" in sources
