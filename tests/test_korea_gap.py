"""Offline end-to-end tests of the standalone Korea gap handoff."""

import importlib.util
import json
import os
import threading
from pathlib import Path
from unittest import mock

import pytest

from lib import korea_gap as gap, naver

FROM = "2026-11-13"
TO = "2026-11-20"
FAKE = naver.Credentials(naver.PROVIDER_LEGACY, "fake-client-id", "fake-client-secret")


def ph(number, name="Tabstack", day="2026-11-18"):
    return {"id": f"ph:{number}", "source": "producthunt", "name": name,
            "tagline": "Private web tools", "url": f"https://example.test/ph/{number}",
            "date": day, "author": "someone"}


def hn(number, title="Show HN: Cursor – an editor", points=50, url=""):
    return {"id": str(number), "title": title, "url": url,
            "hn_url": f"https://news.ycombinator.com/item?id={number}",
            "date": "2026-11-19", "engagement": {"points": points, "comments": 8}}


def gn(number, title="Cursor가 좋은 이유"):
    return {"id": f"gn:{number}", "source": "geeknews", "title": title,
            "url": f"https://news.hada.io/topic?id={number}", "date": TO,
            "summary": "Summary", "author": "neo"}


def fetchers(ph_items=(), hn_items=(), gn_items=()):
    return {"producthunt": lambda: {"items": list(ph_items), "errors": []},
            "show_hn": lambda *args, **kwargs: {"items": list(hn_items), "errors": []},
            "geeknews": lambda: {"items": list(gn_items), "errors": []}}


@pytest.fixture
def bundle():
    return gap.collect(from_date=FROM, to_date=TO, fetchers=fetchers(
        [ph(1), ph(2, "Arc", "2026-11-20")], [hn(3)], [gn(4)]))


def test_collect_order_window_split_overlap():
    result = gap.collect(from_date=FROM, to_date=TO, fetchers=fetchers(
        [ph(1), ph(2, "Arc", TO), ph(8, day="2026-11-12"), ph(9, day="2026-11-21"), ph(10, day=None)],
        [hn(3), hn(5, "Show HN: High, tagline: more", 120), hn(6, "Show HN: AI", 60)],
        [gn(4), gn(7, "Arcade, search, AI 소개"), gn(8, "Arc 브라우저")],
    ))
    assert result["schema"] == 1 and len(result["bundle_id"]) == 32
    assert result["generated_at"].endswith("+00:00")
    assert result["range"] == {"from": FROM, "to": TO}
    items = result["overseas"]
    assert [item["id"] for item in items] == ["hn:5", "hn:6", "hn:3", "ph:2", "ph:1"]
    assert items[0]["name"] == "High" and items[0]["tagline"] == "tagline: more"
    assert items[0]["url"] == items[0]["discussion_url"]
    assert items[1]["tagline"] == ""
    assert items[2]["in_geeknews"] == ["gn:4"]
    assert items[3]["in_geeknews"] == ["gn:8"]
    assert items[4]["points"] is None and items[4]["comments"] is None
    assert result["korea"][0]["matches_overseas"] == ["hn:3"]
    assert result["korea"][1]["matches_overseas"] == []
    assert result["korea"][2]["matches_overseas"] == ["ph:2"]


@pytest.mark.parametrize("separator", [" – ", " — ", " - ", ", ", ": "])
def test_show_hn_split_separators(separator):
    result = gap.collect(from_date=FROM, to_date=TO, fetchers=fetchers(hn_items=[
        hn(1, "Show HN: Name" + separator + "tagline - details", url="https://example.test/product"),
    ]))
    item = result["overseas"][0]
    assert (item["name"], item["tagline"]) == ("Name", "tagline - details")
    assert item["url"] == "https://example.test/product"


def test_collect_sources_run_in_parallel_and_fail_independently():
    barrier = threading.Barrier(3, timeout=5)

    def source(name, response):
        def run(*args, **kwargs):
            barrier.wait()
            if name == "producthunt":
                raise OSError("feed offline")
            if name == "show_hn":
                assert args == (FROM, TO) and kwargs == {"min_points": 77}
            return {"items": response, "errors": []}
        return run

    result = gap.collect(from_date=FROM, to_date=TO, min_points=77, fetchers={
        "producthunt": source("producthunt", []), "show_hn": source("show_hn", [hn(1)]),
        "geeknews": source("geeknews", [gn(2)]),
    })
    assert result["errors"] == {"producthunt": "feed offline"}
    assert len(result["overseas"]) == len(result["korea"]) == 1


def test_collect_all_fail_and_default_fetchers(monkeypatch):
    for module, name in [(gap.producthunt, "fetch_feed"), (gap.hackernews, "fetch_show_hn"), (gap.geeknews, "fetch_feed")]:
        monkeypatch.setattr(module, name, lambda *args, **kwargs: {"items": [], "errors": ["offline"]})
    result = gap.collect(from_date=FROM, to_date=TO)
    assert result["errors"] == {source: "offline" for source in ("producthunt", "show_hn", "geeknews")}
    assert result["overseas"] == result["korea"] == []


def write_picks(tmp_path, bundle, picks=None):
    path = tmp_path / "picks.json"
    path.write_text(json.dumps({"bundle_id": bundle["bundle_id"], "picks": picks if picks is not None else [
        {"id": "ph:1", "queries": ["Tabstack", "탭스택"], "note": "선정 이유"},
    ]}), encoding="utf-8")
    return path


def test_load_picks_duplicate_query_collapse(tmp_path, bundle):
    path = write_picks(tmp_path, bundle, [{"id": "ph:1", "queries": [" Tabstack ", "Tabstack", "탭스택"]}])
    assert gap.load_picks(path, bundle) == [{"id": "ph:1", "queries": ["Tabstack", "탭스택"], "context": [], "note": ""}]


@pytest.mark.parametrize("content", [None, "{broken", "[]", '{"bundle_id":"old","picks":[]}'])
def test_load_picks_file_and_bundle_errors(tmp_path, bundle, content):
    path = tmp_path / "picks.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(gap.PicksError, match="create|regenerate"):
        gap.load_picks(path, bundle)


@pytest.mark.parametrize("picks", [
    [], {}, "bad", [None], [{"id": "missing", "queries": ["x"]}],
    [{"id": [], "queries": ["x"]}],
    [{"id": "ph:1", "queries": ["a"]}, {"id": "ph:1", "queries": ["b"]}],
    [{"id": "ph:1"}], [{"id": "ph:1", "queries": []}],
    [{"id": "ph:1", "queries": ["1", "2", "3", "4"]}],
    [{"id": "ph:1", "queries": "Tabstack"}], [{"id": "ph:1", "queries": [1]}],
    [{"id": "ph:1", "queries": [" "]}], [{"id": "ph:1", "queries": ['""']}],
])
def test_load_picks_validation_remedies(tmp_path, bundle, picks):
    path = write_picks(tmp_path, bundle, picks)
    with pytest.raises(gap.PicksError, match="select|use an id|combine|supply|replace"):
        gap.load_picks(path, bundle)


def mention_result(number=0, *, errors=(), requests=2, prefix="post"):
    return {"matches": [{"kind": "blog" if i % 2 == 0 else "news", "title": f"Sample {i}",
                         "link": f"https://example.test/{prefix}/{i}", "date": TO,
                         "recent": True} for i in range(number)],
            "errors": list(errors), "requests": requests, "skipped_no_context": 0, "fetched": {"blog": 100, "news": 100}}


@pytest.mark.parametrize("number,verdict", [(0, "none"), (1, "rare"), (4, "rare"), (5, "some"), (19, "some"), (20, "arrived")])
def test_check_verdict_edges(bundle, number, verdict):
    with mock.patch.object(naver, "count_mentions", return_value=mention_result(number)) as count:
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    count.assert_called_once_with("Tabstack", FROM, TO, credentials=FAKE, context=[])
    item = result["results"][0]
    assert item["verdict"] == verdict and item["matched"] == number
    assert item["recent"] == number
    assert item["older"] == 0 and item["history"] is False
    assert item["counts"] == {"blog": (number + 1) // 2, "news": number // 2}
    assert len(item["samples"]) == min(3, number)
    assert result["requests_used"] == 2


def test_check_dedupe_by_link_keeps_first_relevance_order(bundle):
    first = mention_result(2)
    first["matches"][1].update(recent=False, date=None)
    second = mention_result(3)
    second["matches"][0]["title"] = "Duplicate title"
    second["matches"][1]["recent"] = True
    with mock.patch.object(naver, "count_mentions", side_effect=[first, second]):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack", "탭스택"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    item = result["results"][0]
    assert result["requests_used"] == 4
    assert item["matched"] == item["recent"] == 3
    assert item["counts"] == {"blog": 2, "news": 1}
    assert item["samples"] == [
        {"title": f"Sample {i}", "date": TO, "kind": "blog" if i % 2 == 0 else "news"}
        for i in range(3)
    ]


@pytest.mark.parametrize("budget,checked,used", [(0, 0, 0), (1, 0, 0), (2, 1, 2), (5, 1, 2), (6, 2, 6), (8, 3, 8)])
def test_check_budget_stops_all_later_picks(bundle, budget, checked, used):
    picks = [{"id": "ph:1", "queries": ["Tabstack"]},
             {"id": "hn:3", "queries": ["Cursor", "커서"]},
             {"id": "ph:2", "queries": ["Arc"]}]
    queries = []

    def counting_fake(query, *args, **kwargs):
        queries.append(query)
        assert 2 * len(queries) <= budget
        return mention_result()

    with mock.patch.object(naver, "count_mentions", side_effect=counting_fake):
        result = gap.check(bundle, picks, credentials=FAKE, from_date=FROM, to_date=TO, budget=budget)
    assert len(result["results"]) == checked
    assert result["requests_used"] == used == 2 * len(queries)
    assert queries == [query for pick in picks[:checked] for query in pick["queries"]]
    assert result["unchecked"] == [{"id": pick["id"], "reason": "budget"} for pick in picks[checked:]]


@pytest.mark.parametrize("responses,verdict,partial", [
    ([mention_result(errors=["blog: down", "news: down"])], "unknown", False),
    ([mention_result(1, errors=["blog: down"])], "rare", True),
    ([mention_result(errors=["blog: down"])], "none", True),
    ([mention_result(errors=["blog: down", "news: down"]), mention_result()], "none", True),
    ([mention_result(errors=["unconfigured"], requests=0)], "unknown", False),
])
def test_check_unknown_partial(bundle, responses, verdict, partial):
    with mock.patch.object(naver, "count_mentions", side_effect=responses):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack", "탭스택"][:len(responses)]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    assert result["results"][0]["verdict"] == verdict
    assert result["results"][0]["partial"] is partial


def test_render_report_and_digest(bundle):
    bundle["errors"] = {"producthunt": "offline"}
    picks = [{"id": "hn:3", "queries": ["Cursor"], "note": "선정 이유"},
             {"id": "ph:1", "queries": ["Tabstack"]}, {"id": "ph:2", "queries": ["Arc"]}]
    with mock.patch.object(naver, "count_mentions", return_value=mention_result(1, errors=["news: down"])):
        result = gap.check(bundle, picks, credentials=FAKE, from_date=FROM, to_date=TO, budget=3)
    report = gap.render_report(bundle, result)
    for heading in [f"# 한국 유입 확인 ({FROM} ~ {TO})", "## 해외에서 뜨는데 한국 글이 적은 것",
                    "## 한국에 조금 알려진 것", "## 이미 한국에 들어온 것", "## 확인하지 못한 것",
                    f"## GeekNews 최신 글 ({bundle['generated_at'].split('T')[0]} 수집, 날짜는 UTC)"]:
        assert heading in report
    for text in ["해당 없음", "네이버: 최근 30일 이름이 들어간 글 1건 (블로그 1, 뉴스 0), 그 전 글 0건",
                 "GeekNews에도 소개됨", "해외 후보와 일치", "네이버 요청: 2 / 3", "수집 오류 (producthunt): offline",
                 "판정 기준(최근 30일 글 수): 없음 0건 · 거의 없음 1–4건 · 조금 있음 5–19건 · 이미 들어옴 20건 이상.", "일부 요청 실패",
                 "요청 예산 부족", "표본: 2026-11-20 Sample 0", "선정 메모: 선정 이유", "50점 · 댓글 8개", "an editor"]:
        assert text in report
    digest = gap.render_digest(bundle, "/tmp/candidates.json")
    assert "hn:3 | show_hn | 2026-11-19 | 50 | Cursor — an editor" in digest
    assert "ph:1 | producthunt | 2026-11-18 | - | Tabstack" in digest
    assert "Cursor가 좋은 이유" in digest and "/tmp/candidates.json" in digest
    assert bundle["bundle_id"] in digest and '"queries"' in digest and "offline" in digest


def test_render_order_inside_verdicts_and_unknown(bundle):
    # Deliberately reverse pick order; reports use source ordering inside each band.
    picks = [{"id": item["id"], "queries": [item["name"]]} for item in reversed(bundle["overseas"])]
    with mock.patch.object(naver, "count_mentions", return_value=mention_result()):
        result = gap.check(bundle, picks, credentials=FAKE, from_date=FROM, to_date=TO)
    report = gap.render_report(bundle, result)
    assert report.index("### Cursor") < report.index("### Arc") < report.index("### Tabstack")
    result["results"][0]["verdict"] = "unknown"
    result["results"][0]["errors"] = ["blog: down", "news: down"]
    result["results"][1]["verdict"] = "none"
    result["results"][2]["verdict"] = "rare"
    report = gap.render_report(bundle, result)
    assert report.index("### Arc") < report.index("### Cursor")
    assert report.index("## 확인하지 못한 것") < report.index("### Tabstack")
    assert "모든 네이버 요청 실패" in report


def test_render_all_empty():
    bundle = gap.collect(from_date=FROM, to_date=TO, fetchers=fetchers())
    result = gap.check(bundle, [], credentials=FAKE, from_date=FROM, to_date=TO)
    assert gap.render_report(bundle, result).count("해당 없음") == 5


@pytest.fixture
def cli(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "skills/last30days/scripts/korea_gap.py"
    spec = importlib.util.spec_from_file_location("korea_gap_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.env, "get_config", lambda: {})
    monkeypatch.setattr(module.dates, "get_date_range", lambda days, as_of_date=None: (FROM, TO))
    return module


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("emit", ["text", "json"])
def test_cli_collect_writes_bundle(cli, tmp_path, bundle, failed, emit, capsys):
    if failed:
        bundle["errors"] = {source: "offline" for source in ("producthunt", "show_hn", "geeknews")}
        bundle["overseas"] = bundle["korea"] = []
    with mock.patch.object(gap, "collect", return_value=bundle) as collect, mock.patch.object(naver, "count_mentions") as count:
        status = cli.main(["collect", "--save-dir", str(tmp_path), "--emit", emit])
    assert status == int(failed)
    count.assert_not_called()
    collect.assert_called_once_with(from_date=FROM, to_date=TO, min_points=50)
    saved = tmp_path / cli.BUNDLE_FILENAME
    assert json.loads(saved.read_text(encoding="utf-8")) == bundle
    if os.name == "posix":
        assert saved.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert captured.err == f"Saved bundle: {saved}\n"
    output = captured.out
    if emit == "json":
        assert json.loads(output) == bundle
    else:
        assert str(saved) in output


@pytest.mark.parametrize("case,remedy", [("no-dir", "--save-dir"), ("no-bundle", "run collect"),
    ("bad-bundle", "run collect"), ("bad-picks", "--picks"), ("no-credentials", "NAVER_CLIENT_SECRET")])
def test_cli_contract_errors_no_requests(cli, tmp_path, bundle, case, remedy, capsys):
    picks = write_picks(tmp_path, bundle)
    if case not in ("no-bundle", "no-dir"):
        (tmp_path / cli.BUNDLE_FILENAME).write_text(json.dumps(bundle) if case != "bad-bundle" else "bad", encoding="utf-8")
    if case == "bad-picks":
        picks.write_text("bad", encoding="utf-8")
    args = ["check", "--picks", str(picks)]
    if case != "no-dir":
        args.extend(["--save-dir", str(tmp_path)])
    with mock.patch.object(gap, "check") as check, mock.patch.object(naver, "_get_rows") as get:
        assert cli.main(args) == 2
    check.assert_not_called()
    get.assert_not_called()
    assert remedy in capsys.readouterr().err


@pytest.mark.parametrize("emit", ["md", "json"])
def test_cli_success_private_report_never_overwrites(cli, tmp_path, bundle, monkeypatch, capsys, emit):
    config = {"NAVER_CLIENT_ID": "fake-client-id", "NAVER_CLIENT_SECRET": "fake-client-secret",
              "LAST30DAYS_MEMORY_DIR": str(tmp_path)}
    monkeypatch.setattr(cli.env, "get_config", lambda: config)
    (tmp_path / cli.BUNDLE_FILENAME).write_text(json.dumps(bundle), encoding="utf-8")
    picks = write_picks(tmp_path, bundle)
    original = tmp_path / f"korea-gap-{TO}.md"
    original.write_text("Keep original", encoding="utf-8")
    calls = []

    def date_range(days, as_of_date=None):
        calls.append(days)
        return FROM, TO

    monkeypatch.setattr(cli.dates, "get_date_range", date_range)
    with mock.patch.object(naver, "count_mentions", return_value=mention_result(1)) as count:
        for suffix in (2, 3):
            assert cli.main(["check", "--picks", str(picks), "--emit", emit]) == 0
            captured = capsys.readouterr()
            saved = tmp_path / f"korea-gap-{TO}-{suffix}.md"
            assert captured.err == f"Saved report: {saved}\n"
            output = captured.out
            assert "Saved report:" not in output
            if emit == "json":
                assert json.loads(output)["requests_used"] == 4
            else:
                assert output == saved.read_text(encoding="utf-8")
            assert "네이버: 최근 30일 이름이 들어간 글 1건" in saved.read_text(encoding="utf-8")
            if os.name == "posix":
                assert saved.stat().st_mode & 0o777 == 0o600
    assert original.read_text(encoding="utf-8") == "Keep original"
    assert calls == [30, 30]
    assert count.call_count == 4
    assert all(call.kwargs["credentials"] == FAKE for call in count.call_args_list)


def test_cli_explicit_save_dir_wins(cli, tmp_path, bundle, monkeypatch):
    monkeypatch.setattr(cli.env, "get_config", lambda: {"LAST30DAYS_MEMORY_DIR": str(tmp_path / "unused")})
    with mock.patch.object(gap, "collect", return_value=bundle):
        assert cli.main(["collect", "--save-dir", str(tmp_path), "--days", "10", "--min-points", "70"]) == 0
    assert (tmp_path / cli.BUNDLE_FILENAME).exists()
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/my_page#x~(part)", "https://example.com/my_page#x~(part)"),
    ("http://example.com/a%20b?q=한글&x=1", "http://example.com/a%20b?q=한글&x=1"),
    ("https://example.com/a b\t\r\n<>`\x00\x7f\x80\u00a0",
     "https://example.com/a%20b%09%0D%0A%3C%3E%60%00%7F%C2%80%C2%A0"),
    ("javascript:alert(1)", "(주소 없음)"),
    ("data:text/html,hello", "(주소 없음)"),
    ("//example.com/path", "(주소 없음)"),
    ("", "(주소 없음)"),
])
def test_report_formats_candidate_and_geeknews_urls(bundle, url, expected):
    candidate = bundle["overseas"][0]
    candidate["url"] = url
    bundle["korea"][0]["url"] = url
    with mock.patch.object(naver, "count_mentions", return_value=mention_result()):
        result = gap.check(bundle, [{"id": candidate["id"], "queries": [candidate["name"]]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    report = gap.render_report(bundle, result)
    # Candidate, overlap note, and GeekNews section must all use the URL formatter.
    assert report.count(expected) == 3
    if expected != url and url:
        assert url not in report
    assert "my\\_page" not in report
    assert "javascript:" not in report


def test_unknown_report_omits_counts_and_samples(bundle):
    with mock.patch.object(naver, "count_mentions", return_value=mention_result(
        errors=["blog: HTTP 403", "news: timeout"],
    )):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    # Suppress even stale samples in an unknown result.
    result["results"][0]["samples"] = [{"title": "Stale sample must not appear", "date": TO, "kind": "blog"}]
    report = gap.render_report(bundle, result)
    assert "네이버: 최근 30일 이름이 들어간 글" not in report
    assert "표본:" not in report and "Stale sample" not in report
    assert report.count("확인 불가:") == 1
    assert "확인 불가: 모든 네이버 요청 실패 또는 인증 미설정. blog: HTTP 403; news: timeout" in report


def test_report_geeknews_overlap_uses_titles_and_urls(bundle):
    candidate = bundle["overseas"][0]
    bundle["korea"][0]["title"] = "Cursor *소개*"
    second = {**gn(5, "Cursor #후기"), "matches_overseas": [candidate["id"]],
              "url": "https://news.hada.io/my_page#x~(part)"}
    bundle["korea"].append(second)
    candidate["in_geeknews"].append(second["id"])
    with mock.patch.object(naver, "count_mentions", return_value=mention_result()):
        result = gap.check(bundle, [{"id": candidate["id"], "queries": [candidate["name"]]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    report = gap.render_report(bundle, result)
    notes = [line for line in report.splitlines() if line.startswith("GeekNews에도 소개됨:")]
    assert notes == [
        r"GeekNews에도 소개됨: Cursor \*소개\* (https://news.hada.io/topic?id=4)",
        r"GeekNews에도 소개됨: Cursor \#후기 (https://news.hada.io/my_page#x~(part))",
    ]
    assert "gn:4" not in report and "gn:5" not in report


def test_geeknews_report_dates_collection_and_older_entries(bundle):
    bundle["generated_at"] = "2026-11-19T23:30:00+00:00"
    bundle["korea"][0]["date"] = "2026-11-17"
    bundle["korea"].append({**gn(5, "날짜 없는 글"), "date": None, "matches_overseas": []})
    result = gap.check(bundle, [], credentials=FAKE, from_date=FROM, to_date=TO)
    report = gap.render_report(bundle, result)
    assert "## GeekNews 최신 글 (2026-11-19 수집, 날짜는 UTC)" in report
    assert "- 2026-11-17 · Cursor가 좋은 이유 — 해외 후보와 일치 · https://news.hada.io/topic?id=4" in report
    assert "- 날짜 없음 · 날짜 없는 글 · https://news.hada.io/topic?id=5" in report
    assert "오늘 GeekNews" not in report
    bundle["korea"] = []
    assert "## GeekNews 최신 글 (2026-11-19 수집, 날짜는 UTC)\n\n해당 없음" in gap.render_report(bundle, result)


def test_cli_utf8_output_survives_legacy_stream_encoding(cli, monkeypatch):
    import io

    out, err = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(cli.sys, "stdout", io.TextIOWrapper(out, encoding="cp1252"))
    monkeypatch.setattr(cli.sys, "stderr", io.TextIOWrapper(err, encoding="cp1252"))
    cli._utf8_output()
    print("한국 유입 확인", file=cli.sys.stdout, flush=True)
    print("저장 위치", file=cli.sys.stderr, flush=True)
    assert out.getvalue().decode("utf-8") == "한국 유입 확인\n"
    assert err.getvalue().decode("utf-8") == "저장 위치\n"


@pytest.mark.parametrize("context,expected", [
    ([], []), (["browser"], ["browser"]),
    (["  web \n browser ", "web browser", "브라우저"], ["web browser", "브라우저"]),
    (["AI", "브라우저", "browser"], ["AI", "브라우저", "browser"]),
])
def test_load_picks_context_normalization(tmp_path, bundle, context, expected):
    path = write_picks(tmp_path, bundle, [{"id": "ph:1", "queries": ["Tabstack"], "context": context}])
    assert gap.load_picks(path, bundle)[0]["context"] == expected


@pytest.mark.parametrize("context", [None, "browser", {}, 1, True, [""], [" \n "], [None], [1], [[]], ["AI", False],
                                      ["a", "b", "c", "d"], ["a", "a", "a", "a"]])
def test_load_picks_invalid_context_has_remedy(tmp_path, bundle, context):
    path = write_picks(tmp_path, bundle, [{"id": "ph:1", "queries": ["Tabstack"], "context": context}])
    with pytest.raises(gap.PicksError, match="context.*supply context words or omit context"):
        gap.load_picks(path, bundle)


@pytest.mark.parametrize("older,history", [(4, False), (5, True), (30, True)])
def test_check_older_posts_do_not_raise_verdict(bundle, older, history):
    response = mention_result(older)
    for index, item in enumerate(response["matches"]):
        item.update(recent=False, date="2020-01-01" if index else None)
    with mock.patch.object(naver, "count_mentions", return_value=response):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    item = result["results"][0]
    assert item["matched"] == item["older"] == older
    assert item["recent"] == 0 and item["verdict"] == "none"
    assert item["counts"] == {"blog": 0, "news": 0}
    assert gap.HISTORY_MIN == 5 and item["history"] is history


def test_check_recent_counts_context_and_skipped_sum(bundle):
    first = mention_result(5)
    first["matches"][0].update(recent=False, date="2000-01-01")
    first["matches"][2].update(recent=False, date=None)
    first["skipped_no_context"] = 7
    second = mention_result(1, prefix="another")
    second["skipped_no_context"] = 3
    context = ["browser", "브라우저"]
    with mock.patch.object(naver, "count_mentions", side_effect=[first, second]) as count:
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack", "탭스택"], "context": context}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    assert count.call_args_list == [mock.call(query, FROM, TO, credentials=FAKE, context=context)
                                    for query in ("Tabstack", "탭스택")]
    item = result["results"][0]
    assert item["matched"] == 6 and item["recent"] == 4 and item["older"] == 2
    assert item["verdict"] == "rare" and item["counts"] == {"blog": 2, "news": 2}
    assert item["context"] == context and item["skipped_no_context"] == 10
    assert result["requests_used"] == 4


@pytest.mark.parametrize("rows,expected", [
    ([("old", "2020-01-02", False), ("recent", "2026-11-18", True),
      ("newest", "2026-11-19", True), ("no date", None, False)], ["newest", "recent", "old"]),
    ([("no date", None, False), ("oldest", "2000-01-01", False),
      ("older", "2020-01-01", False)], ["older", "oldest", "no date"]),
    ([("no date", None, False), ("old", "2020-01-01", False),
      ("recent", "2026-11-18", True)], ["recent", "old", "no date"]),
])
def test_check_samples_date_order_and_shape(bundle, rows, expected):
    response = mention_result(len(rows))
    for item, (title, day, recent) in zip(response["matches"], rows):
        item.update(title=title, date=day, recent=recent)
    with mock.patch.object(naver, "count_mentions", return_value=response):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    by_title = {item["title"]: item for item in response["matches"]}
    assert result["results"][0]["samples"] == [
        {key: by_title[title][key] for key in ("title", "date", "kind")} for title in expected
    ]


def test_report_context_history_samples_and_digest(bundle):
    response = mention_result(7)
    for item in response["matches"]:
        item.update(recent=False, date="2000-01-01")
    response["matches"][0].update(title="Recent *title*", recent=True, date="2026-11-19")
    response["matches"][1].update(title="Second", recent=True, date="2026-11-18")
    response["skipped_no_context"] = 9
    with mock.patch.object(naver, "count_mentions", return_value=response):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack"], "context": ["browser", "브라우저"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    report = gap.render_report(bundle, result)
    assert "네이버: 최근 30일 이름이 들어간 글 2건 (블로그 1, 뉴스 1), 그 전 글 5건" in report
    assert "맥락 단어: browser, 브라우저 (이름만 있고 맥락 단어가 없는 글 9건 제외)" in report
    assert "이 이름은 전부터 쓰였습니다(그 전 글 5건). 기존 브랜드이거나 다른 뜻일 수 있으니 표본을 확인하세요." in report
    assert r"표본: 2026-11-19 Recent \*title\* / 2026-11-18 Second / 2000-01-01 Sample 2" in report
    assert "판정 기준(최근 30일 글 수): 없음 0건 · 거의 없음 1–4건 · 조금 있음 5–19건 · 이미 들어옴 20건 이상." in report
    digest = gap.render_digest(bundle, "/tmp/candidates.json")
    example = json.loads(digest[digest.index('{\n'):])
    assert example["picks"][0]["context"] == []
    assert "이름이 흔한 단어라면 함께 나와야 하는 단어를 선택 항목인 context에 넣으세요." in digest


def test_report_no_context_or_history_and_dateless_sample(bundle):
    response = mention_result(1)
    response["matches"][0].update(recent=False, date=None)
    with mock.patch.object(naver, "count_mentions", return_value=response):
        result = gap.check(bundle, [{"id": "ph:1", "queries": ["Tabstack"]}],
                           credentials=FAKE, from_date=FROM, to_date=TO)
    report = gap.render_report(bundle, result)
    assert "네이버: 최근 30일 이름이 들어간 글 0건 (블로그 0, 뉴스 0), 그 전 글 1건" in report
    assert "맥락 단어:" not in report and "이 이름은 전부터" not in report
    assert "표본: 날짜 없음 Sample 0" in report


def test_cli_check_window_ends_on_the_korean_calendar_day(cli, tmp_path, bundle, monkeypatch):
    from datetime import datetime, timezone

    # 16:00 UTC is 01:00 the next day in Korea; 14:59 UTC is still the same day there.
    assert cli._korea_today(datetime(2026, 11, 19, 16, 0, tzinfo=timezone.utc)) == "2026-11-20"
    assert cli._korea_today(datetime(2026, 11, 19, 14, 59, tzinfo=timezone.utc)) == "2026-11-19"

    monkeypatch.setattr(cli.env, "get_config", lambda: {"NAVER_CLIENT_ID": "dummy-id", "NAVER_CLIENT_SECRET": "dummy-secret"})
    monkeypatch.setattr(cli, "_korea_today", lambda now=None: "2026-11-20")
    (tmp_path / cli.BUNDLE_FILENAME).write_text(json.dumps(bundle), encoding="utf-8")
    picks = write_picks(tmp_path, bundle)
    seen = []

    def date_range(days, as_of_date=None):
        seen.append((days, as_of_date))
        return FROM, TO

    monkeypatch.setattr(cli.dates, "get_date_range", date_range)
    with mock.patch.object(naver, "count_mentions", return_value=mention_result(1)):
        assert cli.main(["check", "--picks", str(picks), "--save-dir", str(tmp_path)]) == 0
    assert seen == [(30, "2026-11-20")]
