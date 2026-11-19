"""Naver source: blog and news retrieval, window filtering, Korean relevance."""

import datetime
import urllib.parse
from unittest import mock

import pytest

from lib import cjk, dedupe, doctor, env, http, naver, normalize, pipeline, planner, render, schema, signals, ui
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
    """Mock http.request with per-kind pages keyed by start.

    Integer keys serve the same rows, in fixture order, for sim and date.
    A (sort, start) key overrides that sort's page; absent pages are empty.
    The fail argument applies to both sorts of the named kind.
    """

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
        assert query["sort"] in {"sim", "date"}
        if kind in self.fail:
            raise self.fail[kind]
        start = int(query["start"])
        rows = self.pages[kind].get((query["sort"], start), self.pages[kind].get(start, []))
        return {"lastBuildDate": "Sun, 11 Oct 2026 10:00:00 +0900", "total": len(rows), "start": int(query["start"]), "display": len(rows), "items": rows}

    def starts(self, kind):
        return [int(call["query"]["start"]) for call in self.calls if call["kind"] == kind]


def _install(monkeypatch, api):
    monkeypatch.setattr(naver.http, "request", api)
    return api


def _search(depth="default", credentials=HUB, to_date=TO_DATE, **kwargs):
    return naver.search_naver(TOPIC, FROM_DATE, to_date, depth=depth, credentials=credentials, **kwargs)


class SortedApi:
    """Mock transport keyed by (kind, sort, start); leaves the old FakeApi intact."""

    def __init__(self, pages=None, failures=()):
        self.pages = pages or {}
        self.failures = set(failures)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        kind = parsed.path.rsplit("/", 1)[-1].removesuffix(".json")
        key = (kind, params["sort"], int(params["start"]))
        self.calls.append(key)
        assert method == "GET"
        assert params["display"] == str(naver.DISPLAY)
        if key in self.failures:
            raise OSError("fake page failure")
        return {"items": self.pages.get(key, [])}

    def sequence(self, kind):
        return [(sort, start) for called_kind, sort, start in self.calls if called_kind == kind]


@pytest.mark.parametrize("kind", naver.KINDS)
def test_historical_sorted_requests_reach_the_ceiling_without_sim(monkeypatch, kind, capsys):
    row = _blog if kind == "blog" else _news
    starts = list(range(1, 1001, 100))
    api = _install(monkeypatch, SortedApi({
        (kind, "date", start): [row(start + offset) for offset in range(naver.DISPLAY)]
        for start in starts
    }))
    result = naver.search_naver(TOPIC, "2026-08-01", "2026-08-31", depth="quick", credentials=HUB)
    assert api.sequence(kind) == [("date", start) for start in starts]
    assert all(sort == "date" for _, sort, _ in api.calls)
    assert result["items"] == []
    assert "stopped at 10 pages; older in-window results may be missing" in capsys.readouterr().err


@pytest.mark.parametrize("stop", ["short", "old"])
def test_historical_date_paging_retains_early_stops(monkeypatch, stop, capsys):
    second = [_blog(101)] if stop == "short" else _full_page(101, "20260801")
    api = _install(monkeypatch, SortedApi({
        ("blog", "date", 1): _full_page(1, "20261010"),
        ("blog", "date", 101): second,
        ("blog", "date", 201): _full_page(201, "20260701"),
    }))
    _search(to_date="2026-09-30")
    assert api.sequence("blog") == [("date", 1), ("date", 101)]
    assert "stopped at" not in capsys.readouterr().err


def test_sim_pages_keep_older_in_window_rows_when_date_page_is_all_new(monkeypatch, capsys):
    old_rows = _full_page(1, "20260801")
    old_rows[0] = _blog("window-start", postdate="20260911")
    new_rows = _full_page(300, "20261011")
    # Fresh rows still pass the floor, but their titles are weaker matches.
    for row in new_rows:
        row["title"] = "휴대폰 사용기"
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): old_rows,
        ("blog", "sim", 101): [
            _blog("middle", postdate="20260920"),
            _blog("future", postdate="20261012"),
            _blog("undated", postdate=""),
        ],
        ("blog", "date", 1): new_rows,
    }))
    result = _search()
    assert [row["link"].rsplit("/", 1)[-1] for row in result["items"][:2]] == ["window-start", "middle"]
    assert all(FROM_DATE <= row["_naver_date"] <= TO_DATE for row in result["items"])
    assert any(row["_naver_date"] == TO_DATE for row in result["items"])
    assert "stopped at" not in capsys.readouterr().err


def test_the_same_link_from_both_sorts_is_kept_once(monkeypatch):
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): [_blog(1)],
        ("blog", "date", 1): [_blog(1), _blog(2)],
    }))
    result = _search()
    assert sorted(row["link"] for row in result["items"]) == [_blog(1)["link"], _blog(2)["link"]]


@pytest.mark.parametrize("size", [0, 1, 99])
def test_short_sim_page_still_requests_the_date_page(monkeypatch, size):
    api = _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): [_blog(n) for n in range(size)],
        ("blog", "sim", 101): _full_page(100, "20261005"),
        ("blog", "date", 1): [_blog("fresh", postdate="20261011")],
    }))
    result = _search(depth="deep")
    assert api.sequence("blog") == [("sim", 1), ("date", 1)]
    assert _blog("fresh")["link"] in {row["link"] for row in result["items"]}


def test_equal_scores_preserve_sim_order_across_pages_before_date_only_rows(monkeypatch):
    first = _full_page(0, "20260801")
    first[0] = _blog("sim-first", postdate="20260911")
    first[1] = _blog("sim-second", postdate="20261005")
    last = _blog("sim-third", postdate="20260920")
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): first,
        ("blog", "sim", 101): [last],
        ("blog", "date", 1): [
            _blog("date-newest", postdate="20261011"),
            first[1], first[0], last,
            _blog("date-older", postdate="20261001"),
        ],
    }))
    result = _search()
    assert [row["link"].rsplit("/", 1)[-1] for row in result["items"]] == [
        "sim-first", "sim-second", "sim-third", "date-newest", "date-older",
    ]
    parsed = naver.parse_naver_response(result, query=TOPIC)
    assert len({item["relevance"] for item in parsed}) == 1
    for item in parsed:
        assert not any(key.startswith("_") for key in item)
        assert item["metadata"] == {"kind": "blog", "blog_url": "blog.naver.com/someone"}


@pytest.mark.parametrize("depth", ["default", "deep"])
def test_equal_score_sim_surplus_reserves_newest_date_only_slots(monkeypatch, depth):
    sim = [_blog(f"sim-{n}", postdate=f"202609{11 + n % 20:02d}") for n in range(60)]
    # Deliberately reverse recency to verify selection, not transport order.
    fresh = [_blog(f"fresh-{n}", postdate=f"202610{n + 1:02d}") for n in range(10)]
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): sim,
        ("blog", "date", 1): [sim[0], *fresh],
    }))
    result = _search(depth=depth)
    keep = naver.KEEP_PER_KIND[depth]
    fresh_slots = int(keep * naver.FRESH_SHARE)
    expected = sim[:keep - fresh_slots] + list(reversed(fresh))[:fresh_slots]
    assert [row["link"] for row in result["items"]] == [row["link"] for row in expected]
    if depth == "default":
        assert fresh_slots == 5


@pytest.mark.parametrize("sim_count", [17, 30])
def test_weaker_date_only_rows_use_general_fill_not_reserved_slots(monkeypatch, sim_count):
    sim = [_blog(f"sim-{n}", postdate="20260920") for n in range(sim_count)]
    fresh = [
        _blog(f"fresh-{n}", title="베트남 여행 유심 구매 안내", postdate=f"202610{n + 1:02d}")
        for n in range(10)
    ]
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): sim,
        ("blog", "date", 1): fresh,
    }))
    result = _search()
    expected = (sim + list(reversed(fresh)))[:20]
    assert [row["link"] for row in result["items"]] == [row["link"] for row in expected]
    # The weaker rows pass the ordinary floor and remain eligible for fill.
    strong, weak = naver.parse_naver_response({"items": [sim[0], fresh[0]]}, query=TOPIC)
    assert strong["relevance"] > weak["relevance"] >= naver.SEARCH_RELEVANCE_FLOOR


def test_higher_score_date_only_row_precedes_lower_score_sim_row(monkeypatch):
    weak = _blog("weak-sim", title="베트남 여행 유심 구매 안내")
    strong = _blog("strong-date", postdate="20260911")
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): [weak],
        ("blog", "date", 1): [strong],
    }))
    result = _search()
    assert [row["link"] for row in result["items"]] == [strong["link"], weak["link"]]
    a, b = naver.parse_naver_response(result, query=TOPIC)
    assert a["relevance"] > b["relevance"]


def test_historical_selection_keeps_score_then_day_order(monkeypatch):
    older = _blog("older", postdate="20260911")
    newer = _blog("newer", postdate="20260929")
    weak = _blog("weak", title="베트남 여행 유심 구매 안내", postdate="20260930")
    api = _install(monkeypatch, SortedApi({
        ("blog", "date", 1): [weak, older, newer],
    }))
    result = _search(to_date="2026-09-30")
    assert api.sequence("blog") == [("date", 1)]
    assert [row["link"] for row in result["items"]] == [newer["link"], older["link"], weak["link"]]


def test_quick_selection_keeps_first_sim_rows_in_api_order(monkeypatch):
    sim = [_blog(n, postdate=f"202609{11 + n % 20:02d}") for n in range(30)]
    api = _install(monkeypatch, SortedApi({("blog", "sim", 1): sim}))
    result = _search(depth="quick")
    assert api.sequence("blog") == [("sim", 1)]
    assert [row["link"] for row in result["items"]] == [row["link"] for row in sim[:10]]


@pytest.mark.parametrize("fresh_count", [0, 2])
def test_unfilled_fresh_slots_fall_back_to_remaining_sim_rows(monkeypatch, fresh_count):
    sim = [_blog(f"sim-{n}") for n in range(30)]
    fresh = [_blog(f"fresh-{n}") for n in range(fresh_count)]
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): sim,
        ("blog", "date", 1): fresh,
    }))
    result = _search()
    expected = sim[:20] if not fresh else sim[:15] + fresh + sim[15:18]
    assert [row["link"] for row in result["items"]] == [row["link"] for row in expected]


@pytest.mark.parametrize("failed_request", [("sim", 101), ("date", 1)])
def test_later_sorted_request_failure_keeps_previous_rows(monkeypatch, failed_request, capsys):
    sort, start = failed_request
    first = _full_page(1, "20261005") if sort == "sim" else [_blog(1)]
    _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): first,
        ("news", "sim", 1): [_news(1)],
    }, failures=[("blog", sort, start)]))
    result = _search()
    assert result["errors"] == []
    assert {row["_naver_kind"] for row in result["items"]} == {"blog", "news"}
    assert _blog(1)["link"] in {row["link"] for row in result["items"]}
    assert f"{sort} page {1 if start == 1 else 2} failed" in capsys.readouterr().err


@pytest.mark.parametrize("failing, surviving", [("blog", "news"), ("news", "blog")])
def test_first_sim_failure_fails_only_that_kind(monkeypatch, failing, surviving):
    api = _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): [_blog(1)],
        ("news", "sim", 1): [_news(1)],
    }, failures=[(failing, "sim", 1)]))
    result = _search()
    assert api.sequence(failing) == [("sim", 1)]
    assert [row["_naver_kind"] for row in result["items"]] == [surviving]
    assert result["errors"] == [f"{failing}: fake page failure"]
    assert "error" not in result


@pytest.mark.parametrize("short_sim", [False, True])
def test_budget_applies_after_first_request_even_across_sorts(monkeypatch, short_sim):
    monkeypatch.setattr(naver, "PAGING_BUDGET_SECONDS", 0.0)
    api = _install(monkeypatch, SortedApi({
        ("blog", "sim", 1): [_blog(1)] if short_sim else _full_page(1, "20261005"),
        ("news", "sim", 1): [_news(1)],
    }))
    result = _search(depth="deep")
    assert api.calls == [("blog", "sim", 1), ("news", "sim", 1)]
    assert result["errors"] == []
    assert {row["_naver_kind"] for row in result["items"]} == {"blog", "news"}


@pytest.mark.parametrize("kind", naver.KINDS)
def test_title_weighting_ranks_korean_title_matches_above_description_only(monkeypatch, kind):
    row = _blog if kind == "blog" else _news
    strong = row("A")
    weak = row("B", title="베트남 여행 유심 구매 안내")
    _install(monkeypatch, SortedApi({(kind, "sim", 1): [weak, strong]}))
    result = _search(depth="quick")
    assert [entry["link"] for entry in result["items"]] == [strong["link"], weak["link"]]
    a, b = naver.parse_naver_response(result, query=TOPIC)
    assert a["relevance"] > b["relevance"] >= naver.SEARCH_RELEVANCE_FLOOR
    old_score = token_overlap_relevance(TOPIC, f"{b['title']} {b['text']}")
    assert b["relevance"] == pytest.approx(0.4 * old_score, abs=0.005)


def test_pipeline_caps_naver_searches_for_three_subqueries(monkeypatch):
    # Three distinct results per fetch also exercise the real thin-result gate.
    search = mock.Mock(return_value={"items": [
        {**_blog(n, title=title), "description": description,
         "_naver_kind": "blog", "_naver_date": "2026-10-05"}
        for n, (title, description) in enumerate([
            ("갤럭시 폴드 후기: 여행 중 배터리와 충전 속도 측정", "화면 밝기를 높이고 동영상을 재생하며 전력 소모를 측정했습니다."),
            ("갤럭시 폴드 후기: 야간 카메라 사진의 노이즈 분석", "어두운 골목에서 촬영한 사진은 색감이 자연스럽고 초점도 정확했습니다."),
            ("갤럭시 폴드 후기: 펼친 화면으로 문서 편집하기", "분할 창에서 표와 문서를 함께 보면서 업무를 처리하니 편리했습니다."),
        ])
    ], "errors": [], "provider": "hub"})
    monkeypatch.setattr(naver, "search_naver", search)
    report = pipeline.run(
        topic=TOPIC, config=dict(FAKE_HUB_CONFIG), depth="default",
        requested_sources=["naver"], web_backend="none", as_of_date=TO_DATE,
        external_plan={
            "intent": "opinion", "freshness_mode": "balanced_recent", "cluster_mode": "none",
            "subqueries": [
                {"label": label, "search_query": f"갤럭시 폴드 {label}",
                 "ranking_query": TOPIC, "sources": ["naver"]}
                for label in ["후기", "배터리", "카메라"]
            ],
        },
    )
    assert len(report.query_plan.subqueries) == 3
    assert all(subquery.sources == ["naver"] for subquery in report.query_plan.subqueries)
    assert search.call_count == 2


@pytest.mark.parametrize("override, expected", [(None, 2), (0, 0), (1, 1), (4, 4)])
def test_naver_fetch_cap_uses_the_existing_generic_override(override, expected):
    config = {} if override is None else {"_max_source_fetches": override}
    assert pipeline._source_fetch_cap("naver", config) == expected


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
        "https://naverapihub.apigw.ntruss.com/search/v1/blog",
        "https://naverapihub.apigw.ntruss.com/search/v1/news",
        "https://naverapihub.apigw.ntruss.com/search/v1/news",
    ]
    # Each short sim page is followed by the date page for that kind.
    for call, sort in zip(api.calls, ("sim", "date", "sim", "date")):
        assert call["headers"] == {
            "X-NCP-APIGW-API-KEY-ID": "fake-hub-id",
            "X-NCP-APIGW-API-KEY": "fake-hub-secret",
        }
        assert call["query"] == {"query": TOPIC, "display": "100", "start": "1", "sort": sort}
        assert "fake-hub" not in call["url"]


def test_developers_requests_use_the_openapi_host_and_headers(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(1)]}))
    result = _search(credentials=LEGACY)
    assert result["provider"] == "legacy"
    assert [call["url"].split("?")[0] for call in api.calls] == [
        "https://openapi.naver.com/v1/search/blog.json",
        "https://openapi.naver.com/v1/search/blog.json",
        "https://openapi.naver.com/v1/search/news.json",
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
    # Historical windows still page by date and stop at the lower bound.
    _search(depth="deep", to_date="2026-09-30")
    assert api.starts("blog") == [1, 101]


def test_a_short_page_ends_paging(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)], 101: _full_page(100, "20261001")}))
    # A short date page ends historical paging entirely.
    _search(depth="deep", to_date="2026-10-10")
    assert api.starts("blog") == [1]


@pytest.mark.parametrize("kind", naver.KINDS)
@pytest.mark.parametrize("depth, sequence", [
    ("quick", [("sim", 1)]),
    ("default", [("sim", 1), ("sim", 101), ("date", 1)]),
    ("deep", [("sim", 1), ("sim", 101), ("sim", 201), ("date", 1)]),
    ("unknown-depth", [("sim", 1), ("sim", 101), ("date", 1)]),
])
def test_a_current_window_pages_by_depth(monkeypatch, depth, sequence, kind):
    row = _blog if kind == "blog" else _news
    pages = {
        (kind, sort, start): [row(start + offset) for offset in range(naver.DISPLAY)]
        for sort, start in sequence
    }
    api = _install(monkeypatch, SortedApi(pages))
    _search(depth=depth)
    assert api.sequence(kind) == sequence


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

    api = _install(monkeypatch, FailsOnPageTwo(blog={1: _full_page(0, "20261010")}))
    result = _search()
    assert api.starts("blog") == [1, 101]
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
    # Both sorts of each kind are fetched once, then reused by the second search.
    assert len(api.calls) == 4
    assert [(call["kind"], call["query"]["sort"], call["query"]["start"]) for call in api.calls] == [
        ("blog", "sim", "1"), ("blog", "date", "1"),
        ("news", "sim", "1"), ("news", "date", "1"),
    ]
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


# --- pipeline: availability -------------------------------------------------

FAKE_HUB_CONFIG = {"NCP_APIGW_API_KEY_ID": "fake-hub-id", "NCP_APIGW_API_KEY": "fake-hub-secret"}
FAKE_LEGACY_CONFIG = {"NAVER_CLIENT_ID": "fake-legacy-id", "NAVER_CLIENT_SECRET": "fake-legacy-secret"}


def _available(config, requested=None):
    return pipeline.available_sources(dict(config), requested, x_pending=False)


def test_naver_is_off_by_default():
    assert "naver" not in _available({})


@pytest.mark.parametrize("keys", [FAKE_HUB_CONFIG, FAKE_LEGACY_CONFIG])
def test_keys_alone_do_not_turn_naver_on(keys):
    assert "naver" not in _available(keys)


@pytest.mark.parametrize("keys", [FAKE_HUB_CONFIG, FAKE_LEGACY_CONFIG])
def test_include_sources_and_search_each_activate_naver_when_keys_are_set(keys):
    assert "naver" in _available({**keys, "INCLUDE_SOURCES": "tiktok, Naver"})
    assert "naver" in _available(keys, ["naver"])


def test_without_credentials_naver_is_unavailable_even_when_asked_for():
    assert "naver" not in _available({"INCLUDE_SOURCES": "naver"})
    assert "naver" not in _available({}, ["naver"])
    # Half a pair is not a credential.
    assert "naver" not in _available({"NCP_APIGW_API_KEY_ID": "fake-hub-id"}, ["naver"])


def test_exclude_sources_beats_the_opt_in():
    config = {**FAKE_HUB_CONFIG, "INCLUDE_SOURCES": "naver", "EXCLUDE_SOURCES": "naver"}
    assert "naver" not in _available(config)
    assert "naver" not in _available(config, ["naver"])


def test_the_search_flag_accepts_naver():
    import last30days as cli

    assert cli.parse_search_flag("reddit,Naver") == ["reddit", "naver"]


# --- pipeline: planning ------------------------------------------------------

def _plan(sources):
    return schema.QueryPlan(
        intent="concept",
        freshness_mode="balanced_recent",
        cluster_mode="none",
        raw_topic=TOPIC,
        subqueries=[schema.SubQuery(label="primary", search_query=TOPIC,
                                    ranking_query=TOPIC, sources=list(sources), weight=1.0)],
        source_weights={source: 1.0 for source in sources},
    )


def test_an_activated_naver_joins_an_engine_plan_that_omits_it():
    plan = _plan(["reddit"])
    pipeline._ensure_naver_in_plan(plan, ["reddit", "naver"])
    assert plan.subqueries[0].sources == ["reddit", "naver"]
    assert plan.source_weights["naver"] == plan.source_weights["reddit"]


def test_an_inactive_naver_is_not_added_to_a_plan():
    plan = _plan(["reddit"])
    pipeline._ensure_naver_in_plan(plan, ["reddit"])
    assert plan.subqueries[0].sources == ["reddit"]
    assert "naver" not in plan.source_weights


def test_an_existing_naver_route_is_left_alone():
    plan = _plan(["reddit"])
    plan.subqueries.append(schema.SubQuery(label="focused", search_query="갤럭시 폴드 내구성",
                                           ranking_query="q", sources=["naver"], weight=1.0))
    pipeline._ensure_naver_in_plan(plan, ["reddit", "naver"])
    assert plan.subqueries[0].sources == ["reddit"]
    assert plan.subqueries[1].sources == ["naver"]


def test_an_activated_naver_joins_a_host_plan_that_omits_it():
    plan = _plan(["reddit"])
    pipeline._ensure_unplanned_lanes_in_host_plan(plan, ["reddit", "naver"])
    assert "naver" in plan.subqueries[0].sources
    assert "naver" in plan.source_weights


def test_a_host_plan_without_an_available_naver_is_untouched():
    plan = _plan(["reddit"])
    pipeline._ensure_unplanned_lanes_in_host_plan(plan, ["reddit"])
    assert plan.subqueries[0].sources == ["reddit"]


# --- pipeline: retrieval -----------------------------------------------------

def _retrieve(config, **overrides):
    subquery = schema.SubQuery(label="primary", search_query="갤럭시 폴드", ranking_query=TOPIC, sources=["naver"])
    return pipeline._retrieve_stream_impl(
        topic=TOPIC,
        subquery=subquery,
        source="naver",
        config=dict(config),
        depth="default",
        date_range=(FROM_DATE, TO_DATE),
        runtime=schema.ProviderRuntime(reasoning_provider="none", planner_model="none", rerank_model="none"),
        mock=False,
        **overrides,
    )


def test_the_pipeline_fetches_and_parses_naver_items(monkeypatch):
    api = _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, news={1: [_news(7)]}))
    items, artifact = _retrieve(FAKE_HUB_CONFIG)
    assert [item["metadata"]["kind"] for item in items] == ["blog", "news"]
    assert artifact == {}
    # The subquery is what is searched; the topic is what relevance is judged on.
    assert {call["query"]["query"] for call in api.calls} == {"갤럭시 폴드"}
    assert all(call["headers"]["X-NCP-APIGW-API-KEY-ID"] == "fake-hub-id" for call in api.calls)
    normalized = normalize.normalize_source_items("naver", items, FROM_DATE, TO_DATE)
    assert [item.container for item in normalized] == ["Naver Blog", "Naver News"]


def test_the_pipeline_keeps_one_kind_when_the_other_fails(monkeypatch):
    _install(monkeypatch, FakeApi(blog={1: [_blog(1)]}, fail=("news",)))
    items, artifact = _retrieve(FAKE_LEGACY_CONFIG)
    assert [item["metadata"]["kind"] for item in items] == ["blog"]
    assert artifact == {}


def test_the_pipeline_reports_a_source_failure_when_every_kind_fails(monkeypatch):
    _install(monkeypatch, FakeApi(fail=("blog", "news")))
    items, artifact = _retrieve(FAKE_HUB_CONFIG)
    assert items == []
    assert artifact["_source_outcome"]["detail"] == "blog: down; news: down"


def test_a_naver_stream_without_credentials_fails_without_a_request(monkeypatch):
    api = _install(monkeypatch, FakeApi())
    items, artifact = _retrieve({})
    assert items == []
    assert "not configured" in artifact["_source_outcome"]["detail"]
    assert api.calls == []


# --- pipeline: mock runs -----------------------------------------------------

def test_mock_naver_rows_normalize():
    subquery = schema.SubQuery(label="primary", search_query="galaxy fold", ranking_query="galaxy fold", sources=["naver"])
    raw, artifact = pipeline._mock_stream_results("naver", subquery)
    assert artifact == {}
    window = pipeline.dates.get_date_range(30)
    items = normalize.normalize_source_items("naver", raw, *window)
    assert [item.container for item in items] == ["Naver Blog", "Naver News"]
    assert all(item.engagement == {} for item in items)


def test_a_mock_run_that_asks_for_naver_puts_it_in_the_report():
    report = pipeline.run(topic="galaxy fold", config={}, depth="quick", requested_sources=["naver"], mock=True)
    assert {item.container for item in report.items_by_source["naver"]} == {"Naver Blog", "Naver News"}


def test_a_mock_run_that_does_not_ask_for_naver_leaves_it_out():
    report = pipeline.run(topic="galaxy fold", config={}, depth="quick", mock=True)
    assert "naver" not in report.items_by_source
    assert all("naver" not in subquery.sources for subquery in report.query_plan.subqueries)


# --- rendering ---------------------------------------------------------------

def test_naver_has_a_label_and_a_footer_row_without_engagement_columns():
    assert render.SOURCE_LABELS["naver"] == "Naver"
    assert render._source_label("naver") == "Naver"
    rows = {key: (label, noun, fields) for key, _emoji, label, noun, fields in render._FOOTER_SOURCES}
    assert rows["naver"] == ("Naver", "result", [])


def test_a_briefing_shows_naver_results():
    report = pipeline.run(topic="galaxy fold", config={}, depth="quick", requested_sources=["naver", "reddit"], mock=True)

    compact = render.render_compact(report)
    assert "Naver: 2 results" in compact
    assert "[naver] galaxy fold news report" in compact
    assert "| Naver News |" in compact
    assert "| Naver Blog |" in compact
    assert "https://blog.naver.com/example/galaxy-fold" in compact

    full = render.render_full(report)
    assert "### Naver (2 items)" in full
    assert "*Naver Blog*" in full


def test_a_briefing_without_naver_does_not_mention_it():
    report = pipeline.run(topic="galaxy fold", config={}, depth="quick", mock=True)
    assert "naver" not in render.render_compact(report).lower()
    assert "naver" not in render.render_full(report).lower()


def test_the_progress_display_names_naver():
    assert "naver" in ui.SOURCE_COMPLETION_ORDER
    assert ui.SOURCE_COMPLETION_META["naver"][:3] == ("Naver", "result", "results")
    assert ui._completion_sources({"naver": 2, "reddit": 1}, None) == ["reddit", "naver"]


# --- doctor ------------------------------------------------------------------

def test_doctor_lists_naver():
    assert "naver" in doctor.SOURCE_ORDER
    assert doctor._SOURCE_BUILDERS["naver"] is doctor._naver_record


def test_doctor_reports_naver_as_unconfigured_and_says_how_to_turn_it_on():
    record = doctor._naver_record({})
    assert record["status"] == "unconfigured"
    assert "NCP_APIGW_API_KEY_ID" in record["fix"]
    assert "INCLUDE_SOURCES" in record["fix"]
    assert "--search naver" in record["fix"]
    assert "NAVER_CLIENT_ID" in record["requires"]


@pytest.mark.parametrize("keys, label", [(FAKE_HUB_CONFIG, "NAVER API HUB"), (FAKE_LEGACY_CONFIG, "Naver Developers")])
def test_doctor_reports_keys_without_the_opt_in_as_opt_in(keys, label):
    record = doctor._naver_record(dict(keys))
    assert record["status"] == "opt-in"
    assert record["fix"] == "add naver to INCLUDE_SOURCES (or request it via --search naver)"
    assert label in record["detail"]


def test_doctor_reports_keys_with_the_opt_in_as_ok():
    record = doctor._naver_record({**FAKE_HUB_CONFIG, "INCLUDE_SOURCES": "naver"})
    assert record["status"] == "ok"
    assert record["fix"] == ""


def test_doctor_does_not_call_an_opt_in_without_keys_ready():
    # The pipeline cannot run Naver without a complete pair; doctor must agree.
    record = doctor._naver_record({"INCLUDE_SOURCES": "naver"})
    assert record["status"] == "unconfigured"
    assert "naver" not in _available({"INCLUDE_SOURCES": "naver"})


def test_doctor_names_a_half_set_pair():
    record = doctor._naver_record({"NCP_APIGW_API_KEY_ID": "fake-hub-id"})
    assert record["status"] == "unconfigured"
    assert record["note"] == "NCP_APIGW_API_KEY_ID is set without NCP_APIGW_API_KEY"


def test_doctor_never_prints_a_key_value():
    config = {**FAKE_HUB_CONFIG, **FAKE_LEGACY_CONFIG, "INCLUDE_SOURCES": "naver"}
    rendered = repr(doctor._naver_record(config))
    for value in (*FAKE_HUB_CONFIG.values(), *FAKE_LEGACY_CONFIG.values()):
        assert value not in rendered


def test_doctor_never_live_probes_naver():
    assert "naver" not in doctor._probeable_sources()
