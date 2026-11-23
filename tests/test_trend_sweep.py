"""Mocked transport, concurrency, handoff files and standalone CLI contracts."""

import importlib.util
import io
import json
import os
import threading
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

import pytest

from lib import http, trend_sources as sources, trend_sweep as sweep
from tests.test_trend_sources import FIXTURES, FORMAT_FIXTURES

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
SOURCE = {s.id: s for s in sources.SOURCES}
CLI_PATH = Path(__file__).resolve().parents[1] / 'skills/last30days/scripts/trend_sweep.py'
spec = importlib.util.spec_from_file_location('trend_sweep_cli', CLI_PATH)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class Response(io.BytesIO):
    status = 200


def response(text):
    return Response(text.encode('utf-8') if isinstance(text, str) else text)


def item(number, source='producthunt', group='overseas', url=None):
    return dict(sources.row(f'Title {number}', f'https://example.test/{number}' if url is None else url,
                            '2026-11-20', 'A summary'), source=source, group=group)


def bundle(items=None):
    items = [item(1)] if items is None else items
    source_rows = []
    for source in dict.fromkeys(e['source'] for e in items):
        rows = [e for e in items if e['source'] == source]
        source_rows.append({'id': source, 'group': rows[0]['group'], 'fetched': len(rows), 'kept': len(rows)})
    return {'schema': 1, 'collected_at': NOW.isoformat(), 'previous_bundle': None,
            'sources': source_rows, 'items': items, 'errors': {}}


@pytest.mark.parametrize('source', sources.SOURCES, ids=lambda s: s.id)
def test_every_source_uses_one_honest_bounded_request(source):
    body = FIXTURES[FORMAT_FIXTURES[source.format]]
    stream = response(body)
    with mock.patch.object(stream, 'read', wraps=stream.read) as read, \
         mock.patch.object(http, 'open_request', return_value=stream) as opened, \
         mock.patch.object(http, 'request', side_effect=AssertionError('wrong transport')):
        result = sweep.collect(sources=[source], now=NOW)
        assert not result['errors']
        assert bool(result['items']) == (source.format != 'reddit')
        opened.assert_called_once()
        request = opened.call_args.args[0]
        assert request.full_url == sources.source_url(source, NOW)
        assert request.get_method() == 'GET'
        assert request.get_header('User-agent') == http.USER_AGENT
        assert 'Mozilla' not in request.get_header('User-agent')
        assert opened.call_args.kwargs['timeout'] == 30
        read.assert_called_once_with(5 * 1024 * 1024 + 1)
        assert getattr(request, http._REDIRECT_GUARD_ATTR)('https://example.test/redirect')
        assert not getattr(request, http._REDIRECT_GUARD_ATTR)('http://example.test/redirect')


@pytest.mark.parametrize('failure', [
    HTTPError('https://example.test', 502, 'Bad gateway', {}, None),
    HTTPError('https://example.test', 504, 'Gateway timeout', {}, None),
    TimeoutError('timed out'),
])
def test_apple_failure_isolated_without_retry(failure):
    def opened(request, **kwargs):
        if 'apple.com' in request.full_url:
            raise failure
        return response(FIXTURES['techmeme'])
    with mock.patch.object(http, 'open_request', side_effect=opened) as get:
        result = sweep.collect(sources=[SOURCE['appstore_kr_top_free'], SOURCE['techmeme']], now=NOW)
    assert get.call_count == 2
    assert len(result['items']) == 1
    assert 'appstore_kr_top_free' in result['errors']
    assert str(failure) in result['errors']['appstore_kr_top_free']


@pytest.mark.parametrize('code', [422, 429, 403, 500])
def test_reddit_failure_no_retry(code):
    error = HTTPError('https://example.test', code, 'fake failure', {}, None)
    with mock.patch.object(http, 'open_request', side_effect=error) as get:
        result = sweep.collect(sources=[SOURCE['reddit_sideproject']], now=NOW)
    get.assert_called_once()
    assert str(code) in result['errors']['reddit_sideproject']


def test_dns_failure_no_retry():
    with mock.patch.object(http, 'open_request', side_effect=OSError('DNS resolution failed')) as get:
        result = sweep.collect(sources=[SOURCE['hn_show']], now=NOW)
    get.assert_called_once()
    assert 'DNS resolution failed' in result['errors']['hn_show']


@pytest.mark.parametrize('scheme', ['https', 'http'])
def test_shared_opener_follows_only_https_redirects(scheme):
    target = f'{scheme}://feeds.feedburner.com/geeknews-feed'
    requests = []

    class FakeHTTPS(HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            headers = Message()
            if request.full_url == SOURCE['geeknews'].url:
                headers['Location'] = target
                result = addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                result.msg = 'Found'
            else:
                assert request.full_url == target
                result = addinfourl(io.BytesIO(FIXTURES['geeknews'].encode('utf-8')),
                                   headers, request.full_url, 200)
                result.msg = 'OK'
            return result

    # Exercise the real redirect handler/opener; only the HTTPS I/O is fake.
    opener = build_opener(ProxyHandler({}), http._StripAuthOnCrossOriginRedirect(), FakeHTTPS())
    with mock.patch.object(http, '_opener', opener), \
         mock.patch.object(http, 'open_request', wraps=http.open_request) as opened:
        result = sweep.collect(sources=[SOURCE['geeknews']], now=NOW)
    opened.assert_called_once()
    if scheme == 'https':
        assert [r.full_url for r in requests] == [SOURCE['geeknews'].url, target]
        assert not result['errors']
        assert result['items'][0]['url'] == 'https://news.hada.io/topic?id=37506'
        assert all(r.get_header('User-agent') == http.USER_AGENT for r in requests)
        assert not getattr(requests[-1], http._REDIRECT_GUARD_ATTR)('http://example.test')
    else:
        assert len(requests) == 1
        assert not result['items']
        assert 'redirect refused' in result['errors']['geeknews']


@pytest.mark.parametrize('size,valid', [(5 * 1024 * 1024, True), (5 * 1024 * 1024 + 1, False)])
def test_response_size_boundary(size, valid):
    body = b'x' * size
    with mock.patch.object(http, 'open_request', return_value=response(body)):
        if valid:
            assert len(sweep.fetch_source(SOURCE['hn_show'], NOW)) == size
        else:
            with pytest.raises(ValueError, match='5 MB'):
                sweep.fetch_source(SOURCE['hn_show'], NOW)


@pytest.mark.parametrize('name', ['producthunt', 'geeknews', 'techmeme'])
@pytest.mark.parametrize('prefix', ['<!DOCTYPE feed>', '<!ENTITY x "bad">', '<!doctype rss>', '\x00'])
def test_xml_rejected_on_common_fetch_path(name, prefix):
    with mock.patch.object(http, 'open_request', return_value=response(prefix + FIXTURES[name])):
        result = sweep.collect(sources=[SOURCE[name]], now=NOW)
    assert 'XML DOCTYPE, ENTITY' in result['errors'][name]


def test_non_200_and_bad_json_are_source_errors():
    stream = response('bad body')
    stream.status = 503
    with mock.patch.object(http, 'open_request', return_value=stream):
        assert 'HTTP 503' in sweep.collect(sources=[SOURCE['lobsters']], now=NOW)['errors']['lobsters']
    with mock.patch.object(http, 'open_request', return_value=response('bad JSON')):
        assert 'JSONDecodeError' in sweep.collect(sources=[SOURCE['lobsters']], now=NOW)['errors']['lobsters']


def test_concurrent_collection_deduplicates_in_registry_order():
    barrier = threading.Barrier(3)
    chosen = [SOURCE['devto_top_week'], SOURCE['devto_ai_week'], SOURCE['geeknews']]
    def fetch(source, now):
        barrier.wait(timeout=5)
        return FIXTURES['geeknews'] if source.id == 'geeknews' else FIXTURES['devto_top_week']
    with mock.patch.object(sweep, 'fetch_source', side_effect=fetch):
        result = sweep.collect(sources=chosen, now=NOW)
    assert not result['errors']
    assert [e['source'] for e in result['items']] == ['devto_top_week', 'geeknews']
    assert [(s['fetched'], s['kept']) for s in result['sources']] == [(1, 1), (1, 0), (1, 1)]


def test_collection_does_not_apply_summary_limit():
    data = json.loads(FIXTURES['devto_top_week'])
    rows = [dict(data[0], url=f'https://example.test/{i}') for i in range(65)]
    with mock.patch.object(sweep, 'fetch_source', return_value=json.dumps(rows)):
        result = sweep.collect(sources=[SOURCE['devto_top_week']], now=NOW)
    assert len(result['items']) == 65
    summary = sweep.render_summary(result, 'overseas')
    assert summary.count('\n- ') == 30
    assert '35 more in JSON' in summary


def test_save_summary_limit_groups_private_permissions_and_paths(tmp_path):
    data = bundle([*[item(i) for i in range(35)], item('kr', 'geeknews', 'korea'),
                   item('meme', 'knowyourmeme_news', 'video')])
    saved = sweep.save_bundle(data, tmp_path, per_source=2)
    assert set(saved) == {'json', 'overseas', 'korea', 'video'}
    loaded = json.loads(saved['json'].read_text(encoding='utf-8'))
    assert len(loaded['items']) == 37
    assert all(not e['new'] for e in loaded['items'])
    for group in sources.GROUPS:
        text = saved[group].read_text(encoding='utf-8')
        assert text.endswith(sweep.GUIDANCE)
        assert saved[group].name == f'trend-sweep-2026-11-20-{group}.md'
    overseas = saved['overseas'].read_text()
    assert overseas.count('\n- ') == 2
    assert 'Title kr' not in overseas and 'Title meme' not in overseas
    assert 'Title kr' in saved['korea'].read_text()
    assert 'Title meme' in saved['video'].read_text()
    if os.name == 'posix':
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in saved.values())


def test_new_compares_only_previous_bundle_including_same_day(tmp_path):
    first = sweep.save_bundle(bundle([item(1), item('gone')]), tmp_path)
    original = {k: p.read_bytes() for k, p in first.items()}
    second = sweep.save_bundle(bundle([item(1), item(2)]), tmp_path)
    assert second['json'].name == 'trend-sweep-2026-11-20-2.json'
    data = json.loads(second['json'].read_text())
    assert data['previous_bundle'] == str(first['json'])
    assert [e['new'] for e in data['items']] == [False, True]
    assert 'NEW · Title 2' in second['overseas'].read_text()
    assert 'NEW · Title 1' not in second['overseas'].read_text()
    third = sweep.save_bundle(bundle([item('gone')]), tmp_path)
    assert third['json'].name == 'trend-sweep-2026-11-20-3.json'
    assert json.loads(third['json'].read_text())['items'][0]['new'] is True
    assert all(p.read_bytes() == original[k] for k, p in first.items())


def test_new_title_fallback():
    data = bundle([item('same', url=''), item('new', url='')])
    sweep.mark_new(data, bundle([item('same', url='')]))
    assert [e['new'] for e in data['items']] == [False, True]
    sweep.mark_new(data, None)
    assert not any(e['new'] for e in data['items'])


def test_previous_bundle_excludes_unrelated_files_and_numeric_tiebreak(tmp_path):
    for name in ('trend-sweep-2026-11-20-2.json', 'trend-sweep-2026-11-20-10.json'):
        path = tmp_path / name
        path.write_text(json.dumps(bundle()), encoding='utf-8')
        os.utime(path, ns=(1000, 1000))
    for name in ('trend-sweep-2026-11-20-youtube.json', 'korea-gap-candidates.json', 'other.json'):
        (tmp_path / name).write_text('unrelated')
    path, data = sweep.previous_bundle(tmp_path)
    assert path.name == 'trend-sweep-2026-11-20-10.json'
    assert data['schema'] == 1


def test_previous_bundle_uses_most_recent_save_not_old_historical_items(tmp_path):
    old = tmp_path / 'trend-sweep-2026-11-19.json'
    new = tmp_path / 'trend-sweep-2026-11-20.json'
    for path in (old, new):
        path.write_text(json.dumps(bundle()))
    os.utime(old, ns=(1000, 1000))
    os.utime(new, ns=(2000, 2000))
    assert sweep.previous_bundle(tmp_path)[0] == new


def test_corrupt_previous_bundle_stops_without_overwrite(tmp_path):
    old = tmp_path / 'trend-sweep-2026-11-19.json'
    old.write_text('{"schema": 1, "items": [null]}')
    with pytest.raises(ValueError, match='Invalid previous bundle'):
        sweep.save_bundle(bundle(), tmp_path)
    assert list(tmp_path.iterdir()) == [old]


def test_summary_markdown_neutralized_and_urls_preserved():
    data = bundle()
    e = data['items'][0]
    e['title'] = '# [click](bad)\n<script> **bold** `code` &lt;img&gt;'
    e['summary'] = '> quote | table'
    e['url'] = 'https://example.test/a_(b)?a=1&b=2#anchor'
    text = sweep.render_summary(data, 'overseas')
    assert '\\# \\[click\\](bad)' in text
    assert '\\<script\\>' in text and '<script>' not in text
    assert '&lt;img&gt;' in text
    assert '&amp;lt;' not in text
    assert f'<{e["url"]}>' in text
    assert text.endswith(sweep.GUIDANCE)
    e['url'] = 'https://example.test/>' + '\n# injected'
    text = sweep.render_summary(data, 'overseas')
    assert '<https://example.test/%3E%0A#%20injected>' in text
    assert '\n# injected' not in text
    e['url'] = 'javascript:alert(1)'
    assert 'javascript:' not in sweep.render_summary(data, 'overseas')


def test_existing_summary_collision_preserved(tmp_path):
    existing = tmp_path / 'trend-sweep-2026-11-20-korea.md'
    existing.write_text('keep me')
    paths = sweep.save_bundle(bundle(), tmp_path)
    assert paths['json'].name == 'trend-sweep-2026-11-20-2.json'
    assert existing.read_text() == 'keep me'
    assert not (tmp_path / 'trend-sweep-2026-11-20.json').exists()


def test_publish_failure_cleans_only_new_reservations(tmp_path):
    old = sweep.save_bundle(bundle(), tmp_path)
    with mock.patch.object(sweep.private_files, 'replace_private_text', side_effect=OSError('disk full')):
        with pytest.raises(OSError, match='disk full'):
            sweep.save_bundle(bundle(), tmp_path)
    assert set(tmp_path.iterdir()) == set(old.values())


def test_cli_collect_saves_and_reports_without_dumping_content(tmp_path, capsys):
    with mock.patch.object(sweep, 'collect', side_effect=lambda: bundle()), \
         mock.patch.object(cli.env, 'get_config', side_effect=AssertionError('must not open credential stores')):
        assert cli.main(['collect', '--save-dir', str(tmp_path), '--per-source', '1']) == 0
        assert cli.main(['collect', '--save-dir', str(tmp_path)]) == 0
    stdout = capsys.readouterr().out
    assert 'producthunt: 1 fetched, 1 kept' in stdout
    assert 'Title 1' not in stdout
    assert stdout.count('Saved:') == 8
    assert str(tmp_path / 'trend-sweep-2026-11-20-2-video.md') in stdout


def test_cli_memory_directory_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('LAST30DAYS_MEMORY_DIR', str(tmp_path))
    with mock.patch.object(sweep, 'collect', return_value=bundle()):
        assert cli.main(['collect']) == 0
    assert (tmp_path / 'trend-sweep-2026-11-20.json').exists()


def test_cli_memory_directory_from_dotenv(tmp_path):
    cli.env.CONFIG_FILE.write_text(f'LAST30DAYS_MEMORY_DIR={tmp_path}\n', encoding='utf-8')
    # A keyless tool must find its save directory without opening Keychain or pass.
    with mock.patch.object(sweep, 'collect', return_value=bundle()), \
         mock.patch.object(cli.env, '_load_keychain', side_effect=AssertionError('no Keychain')), \
         mock.patch.object(cli.env, '_load_pass', side_effect=AssertionError('no pass store')):
        assert cli.main(['collect']) == 0
    assert (tmp_path / 'trend-sweep-2026-11-20.json').exists()


def test_cli_sources_needs_no_config_or_network(capsys):
    with mock.patch.object(cli.env, 'get_config', side_effect=AssertionError('must not read config')), \
         mock.patch.object(http, 'open_request', side_effect=AssertionError('must not fetch')):
        assert cli.main(['sources']) == 0
    text = capsys.readouterr().out
    assert len(text.splitlines()) == len(sources.SOURCES) == 40
    assert all(s.id in text for s in sources.SOURCES)


def test_cli_missing_directory_returns_2_before_collection(capsys):
    with mock.patch.object(cli.env, 'get_config', side_effect=AssertionError('must not open credential stores')), \
         mock.patch.object(sweep, 'collect', side_effect=AssertionError('must not collect')):
        assert cli.main(['collect']) == 2
        assert cli.main(['collect', '--save-dir', ' ']) == 2
    assert 'Provide --save-dir' in capsys.readouterr().err


def test_cli_all_failed_returns_1_and_saves_errors(tmp_path, capsys):
    with mock.patch.object(sweep, 'fetch_source', side_effect=TimeoutError('fake timeout')):
        failed = sweep.collect(now=NOW)
    with mock.patch.object(sweep, 'collect', return_value=failed):
        assert cli.main(['collect', '--save-dir', str(tmp_path)]) == 1
    assert len(failed['errors']) == 40
    assert 'fake timeout' in capsys.readouterr().out
    assert len(json.loads((tmp_path / 'trend-sweep-2026-11-20.json').read_text())['errors']) == 40


def test_cli_partial_failure_returns_0(tmp_path, capsys):
    data = bundle()
    data['sources'].append({'id': 'appstore_us_top_free', 'group': 'overseas', 'fetched': 0, 'kept': 0})
    data['errors']['appstore_us_top_free'] = 'HTTP 502'
    with mock.patch.object(sweep, 'collect', return_value=data):
        assert cli.main(['collect', '--save-dir', str(tmp_path)]) == 0
    assert 'HTTP 502' in capsys.readouterr().out


def test_cli_utf8_output():
    stdout, stderr = mock.Mock(), mock.Mock()
    with mock.patch.object(cli.sys, 'stdout', stdout), mock.patch.object(cli.sys, 'stderr', stderr):
        cli._utf8_output()
    for stream in (stdout, stderr):
        stream.reconfigure.assert_called_once_with(encoding='utf-8', errors='replace')


def test_cli_full_registry_end_to_end_with_mock_responses(tmp_path, capsys):
    url_sources = {sources.source_url(s, NOW): s for s in sources.SOURCES}
    def opened(request, **kwargs):
        source = url_sources[request.full_url]
        return response(FIXTURES[FORMAT_FIXTURES[source.format]])
    collect = sweep.collect
    with mock.patch.object(http, 'open_request', side_effect=opened) as get, \
         mock.patch.object(sweep, 'collect', side_effect=lambda: collect(now=NOW)):
        assert cli.main(['collect', '--save-dir', str(tmp_path)]) == 0
    assert get.call_count == 40
    data = json.loads((tmp_path / 'trend-sweep-2026-11-20.json').read_text())
    assert not data['errors']
    assert len(data['sources']) == 40 and data['items']
    assert len({e['url'] for e in data['items']}) == len(data['items'])
    stdout = capsys.readouterr().out
    assert all(f'{source.id}:' in stdout for source in sources.SOURCES)
    assert stdout.count('Saved:') == 4


def test_google_trends_shared_urls_keep_all_terms():
    with mock.patch.object(sweep, 'fetch_source', return_value=FIXTURES['google_trends_kr']):
        data = sweep.collect(sources=[SOURCE['google_trends_kr']], now=NOW)
    assert not data['errors']
    assert [e['title'] for e in data['items']] == ['빅뱅', '빅뱅 신곡', '대상 공원 빅 트리']
    assert len({e['url'] for e in data['items']}) == 1
    assert data['sources'][0]['fetched'] == data['sources'][0]['kept'] == 3


def test_google_trends_new_compares_shared_url_and_title(tmp_path):
    import xml.etree.ElementTree as ET

    def feed(terms):
        root = ET.fromstring(FIXTURES['google_trends_kr'])
        channel = root.find('channel')
        for index, entry in enumerate(list(channel)):
            if index >= len(terms):
                channel.remove(entry)
            else:
                entry.find('title').text = terms[index]
        return ET.tostring(root, encoding='unicode')

    for terms in [('A', 'B'), ('B', 'C')]:
        with mock.patch.object(sweep, 'fetch_source', return_value=feed(terms)):
            data = sweep.collect(sources=[SOURCE['google_trends_kr']], now=NOW)
        paths = sweep.save_bundle(data, tmp_path)
    saved = json.loads(paths['json'].read_text())
    assert [(e['title'], e['new']) for e in saved['items']] == [('B', False), ('C', True)]
    text = paths['korea'].read_text()
    assert 'NEW · C' in text and 'NEW · B' not in text


def test_new_counts_shared_urls_within_sources_only():
    url = 'https://example.test/shared'
    previous = bundle([item('old', url=url), item('other', source='geeknews', group='korea', url=url)])
    current = bundle([item('renamed', url=url)])
    sweep.mark_new(current, previous)
    assert current['items'][0]['new'] is False


def test_empty_url_deduplication_and_new_share_title_key():
    rows = [item('same', url=''), item('same', url=''), item('different', url='')]
    with mock.patch.object(sweep, 'fetch_source', return_value='unused'), \
         mock.patch.object(sources, 'parse_source', return_value=rows):
        current = sweep.collect(sources=[SOURCE['producthunt']], now=NOW)
    assert [e['title'] for e in current['items']] == ['Title same', 'Title different']
    sweep.mark_new(current, bundle([item('same', url='')]))
    assert [e['new'] for e in current['items']] == [False, True]


def test_tracking_changes_do_not_change_deduplication_or_new():
    template = json.loads(FIXTURES['devto_top_week'])[0]
    def fetch(source, now):
        return json.dumps([dict(template, title=source.id,
                               url=f'https://example.test/post?keep=1&utm_source={source.id}')])
    with mock.patch.object(sweep, 'fetch_source', side_effect=fetch):
        current = sweep.collect(sources=[SOURCE['devto_top_week'], SOURCE['devto_ai_week']], now=NOW)
    assert len(current['items']) == 1
    assert current['items'][0]['url'] == 'https://example.test/post?keep=1'
    assert [s['kept'] for s in current['sources']] == [1, 0]
    previous = bundle([item('old title', url='https://example.test/post?utm_campaign=old&keep=1')])
    sweep.mark_new(current, previous)
    assert current['items'][0]['new'] is False


def test_reddit_fetched_count_is_after_filtering():
    from tests.test_trend_sources import reddit_filter_fixture

    with mock.patch.object(sweep, 'fetch_source', return_value=reddit_filter_fixture()):
        data = sweep.collect(sources=[SOURCE['reddit_sideproject']], now=NOW)
    assert not data['errors']
    assert data['sources'][0]['fetched'] == data['sources'][0]['kept'] == 4
    summary = sweep.render_summary(data, 'overseas', per_source=2)
    assert summary.index('newer-high') < summary.index('older-high')
    assert 'middle' not in summary and 'removed' not in summary


@pytest.mark.parametrize('text,expected', [
    ('Next.js google-labs-code/stitch-skills 500+ ! ().~-', 'Next.js google-labs-code/stitch-skills 500+ ! ().~-'),
    ('`code`', r'\`code\`'),
    ('[link](https://example.test)', r'\[link\](https://example.test)'),
    ('![image](https://example.test)', r'!\[image\](https://example.test)'),
    ('# heading\n## next', r'\# heading \#\# next'),
    ('| a | b |', r'\| a \| b \|'),
    ('<script>raw</script>', r'\<script\>raw\</script\>'),
    ('**bold** _emphasis_', r'\*\*bold\*\* \_emphasis\_'),
    ('{data} \\ path', r'\{data\} \\ path'),
    ('A & B &lt;tag&gt;', 'A & B &lt;tag&gt;'),
    ('line\r\nnext\tword\x00\x1b\x7f', 'line next word'),
])
def test_markdown_uses_korea_gap_escape_set(text, expected):
    assert sweep._markdown(text) == expected


@pytest.mark.parametrize('label,value,expected', [
    ('rank', 1, 'rank 1'), ('points', 532, 'points 532'), ('likes', 5015, 'likes 5015'),
    ('stars this week', 2815, 'stars this week 2815'), ('views', 393338, 'views 393338'),
    ('approx_traffic', '500+', r'approx\_traffic 500+'),
])
def test_summary_metrics_label_before_value(label, value, expected):
    data = bundle()
    data['items'][0]['metric'] = {'label': label, 'value': value}
    assert f' · {expected}' in sweep.render_summary(data, 'overseas')


@pytest.mark.parametrize('old_terms,new_terms,expected', [
    (['A', 'B'], ['B'], [False]),
    (['B'], ['A', 'B'], [True, False]),
    (['A', 'B'], ['B', 'C'], [False, True]),
    (['old title'], ['edited title'], [False]),
])
def test_new_disambiguates_consistently_across_bundles(tmp_path, old_terms, new_terms, expected):
    def trends(terms):
        return bundle([item(term, source='google_trends_kr', group='korea',
                            url='https://trends.google.com/trending/rss?geo=KR') for term in terms])
    sweep.save_bundle(trends(old_terms), tmp_path)
    paths = sweep.save_bundle(trends(new_terms), tmp_path)
    data = json.loads(paths['json'].read_text())
    assert [e['new'] for e in data['items']] == expected


def test_zero_byte_bundle_does_not_block_collect_or_become_previous(tmp_path):
    leftover = tmp_path / 'trend-sweep-2026-11-20.json'
    leftover.touch()
    assert sweep.previous_bundle(tmp_path) == (None, None)
    with mock.patch.object(sweep, 'collect', return_value=bundle()):
        assert cli.main(['collect', '--save-dir', str(tmp_path)]) == 0
    saved = tmp_path / 'trend-sweep-2026-11-20-2.json'
    data = json.loads(saved.read_text())
    assert data['previous_bundle'] is None
    assert not any(e['new'] for e in data['items'])
    assert leftover.read_bytes() == b''  # Never overwrite an existing file.
    assert sweep.previous_bundle(tmp_path)[0] == saved


def test_previous_selection_ignores_empty_and_temporary_leftovers(tmp_path):
    paths = sweep.save_bundle(bundle(), tmp_path)
    leftovers = ['trend-sweep-2026-11-20-2.json', '.trend-sweep-2026-11-20-2.fake.tmp',
                 'trend-sweep-2026-11-20-2.json.tmp', 'trend-sweep-2026-11-20-2.lock']
    for name in leftovers:
        path = tmp_path / name
        path.write_text('' if name.endswith('.json') else 'unfinished')
        os.utime(path, ns=(2000000000000000000, 2000000000000000000))
    assert sweep.previous_bundle(tmp_path)[0] == paths['json']


@pytest.mark.parametrize('contents', ['not json', ' ', '{}', '{"schema": 1, "items": [null]}'])
def test_nonempty_invalid_previous_bundle_still_exits_2(tmp_path, capsys, contents):
    path = tmp_path / 'trend-sweep-2026-11-20.json'
    path.write_text(contents)
    with mock.patch.object(sweep, 'collect', side_effect=AssertionError('must not collect')):
        assert cli.main(['collect', '--save-dir', str(tmp_path)]) == 2
    assert 'Invalid previous bundle' in capsys.readouterr().err
    assert path.read_text() == contents


@pytest.mark.parametrize('failure', [OSError('disk full'), KeyboardInterrupt()])
@pytest.mark.parametrize('stage', ['summary', 'json', 'publish'])
def test_failed_or_interrupted_save_cleans_reservations_and_temps(tmp_path, failure, stage):
    replace = sweep.private_files.replace_private_text
    publish = sweep._publish_json

    def replace_then_fail(path, content):
        assert not (tmp_path / 'trend-sweep-2026-11-20.json').exists()
        replace(path, content)
        if (stage == 'summary' and path.name.endswith('-korea.md')) or (stage == 'json' and path.suffix == '.tmp'):
            raise failure

    def publish_or_fail(staged, destination):
        assert json.loads(staged.read_text())['schema'] == 1
        assert not destination.exists()
        if stage == 'publish':
            raise failure
        publish(staged, destination)

    with mock.patch.object(sweep.private_files, 'replace_private_text', side_effect=replace_then_fail), \
         mock.patch.object(sweep, '_publish_json', side_effect=publish_or_fail):
        with pytest.raises(type(failure)):
            sweep.save_bundle(bundle(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_two_overlapping_saves_expose_only_complete_json(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ready = threading.Event()
    release = threading.Event()
    replace = sweep.private_files.replace_private_text

    def paused_summary(path, content):
        if path.name == 'trend-sweep-2026-11-20-overseas.md':
            ready.set()
            assert release.wait(timeout=10)
        replace(path, content)

    with mock.patch.object(sweep.private_files, 'replace_private_text', side_effect=paused_summary):
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_save = pool.submit(sweep.save_bundle, bundle([item('first')]), tmp_path)
            try:
                assert ready.wait(timeout=10)
                assert not (tmp_path / 'trend-sweep-2026-11-20.json').exists()
                assert sweep.previous_bundle(tmp_path) == (None, None)
                second = sweep.save_bundle(bundle([item('second')]), tmp_path)
                second_bytes = {key: path.read_bytes() for key, path in second.items()}
                assert second['json'].name == 'trend-sweep-2026-11-20-2.json'
                assert sweep.previous_bundle(tmp_path)[0] == second['json']
            finally:
                release.set()
            first = first_save.result(timeout=10)
    assert first['json'].name == 'trend-sweep-2026-11-20.json'
    assert json.loads(first['json'].read_text())['items'][0]['title'] == 'Title first'
    assert all(path.read_bytes() == second_bytes[key] for key, path in second.items())
    assert set(tmp_path.iterdir()) == set(first.values()) | set(second.values())


def test_exclusive_publish_collision_preserves_other_writer(tmp_path):
    publish = sweep._publish_json
    other_content = json.dumps(bundle([item('other writer')]))

    def publish_with_collision(staged, destination):
        if destination.name == 'trend-sweep-2026-11-20.json':
            destination.write_text(other_content)
        publish(staged, destination)

    with mock.patch.object(sweep, '_publish_json', side_effect=publish_with_collision):
        paths = sweep.save_bundle(bundle(), tmp_path)
    assert paths['json'].name == 'trend-sweep-2026-11-20-2.json'
    other = tmp_path / 'trend-sweep-2026-11-20.json'
    assert other.read_text() == other_content
    assert set(tmp_path.iterdir()) == {other, *paths.values()}


def test_unavailable_hard_links_fail_cleanly_on_posix(tmp_path):
    import errno

    if os.name != 'posix':
        pytest.skip('POSIX fallback contract')
    with mock.patch.object(sweep.os, 'link', side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')):
        with pytest.raises(OSError, match='hard links unavailable'):
            sweep.save_bundle(bundle(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_windows_publish_fallback_uses_nonoverwriting_rename(tmp_path):
    import errno
    from types import SimpleNamespace

    staged = tmp_path / 'staged.tmp'
    staged.write_text('complete JSON bytes')
    destination = tmp_path / 'bundle.json'

    # Simulate Windows' no-overwrite rename semantics without changing os.name
    # globally (which would also change pathlib's platform selection).
    def windows_rename(source, target):
        if target.exists():
            raise FileExistsError(target)
        source.rename(target)

    fake_os = SimpleNamespace(name='nt',
                              link=mock.Mock(side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')),
                              rename=mock.Mock(side_effect=windows_rename))
    with mock.patch.object(sweep, 'os', fake_os):
        sweep._publish_json(staged, destination)
        assert destination.read_text() == 'complete JSON bytes'
        staged.write_text('another bundle')
        with pytest.raises(FileExistsError):
            sweep._publish_json(staged, destination)
    assert destination.read_text() == 'complete JSON bytes'
    assert staged.read_text() == 'another bundle'
    assert fake_os.rename.call_count == 2


def test_hn_cross_source_shared_url_drops_identical_pair_only():
    template = json.loads(FIXTURES['hn_front_page'])['hits'][0]
    url = 'https://example.test/shared-story'

    def fetch(source, now):
        titles = ['Same story'] if source.id == 'hn_front_page' else ['Same story', 'Different title']
        return json.dumps({'hits': [dict(template, title=title, url=url) for title in titles]})

    with mock.patch.object(sweep, 'fetch_source', side_effect=fetch):
        data = sweep.collect(sources=[SOURCE['hn_front_page'], SOURCE['hn_week_top']], now=NOW)
    assert not data['errors']
    assert [(e['source'], e['title']) for e in data['items']] == [
        ('hn_front_page', 'Same story'), ('hn_week_top', 'Different title')]
    assert [(s['fetched'], s['kept']) for s in data['sources']] == [(1, 1), (2, 1)]


def test_hn_unique_cross_source_url_drops_second_despite_different_title():
    template = json.loads(FIXTURES['hn_front_page'])['hits'][0]

    def fetch(source, now):
        return json.dumps({'hits': [dict(template, title=source.id, url='https://example.test/story')]})

    with mock.patch.object(sweep, 'fetch_source', side_effect=fetch):
        data = sweep.collect(sources=[SOURCE['hn_front_page'], SOURCE['hn_week_top']], now=NOW)
    assert not data['errors']
    assert [e['title'] for e in data['items']] == ['hn_front_page']
    assert [s['kept'] for s in data['sources']] == [1, 0]


@pytest.mark.parametrize('old_titles,new_titles,expected', [
    (['A', 'B'], ['B'], [False]),
    (['A', 'B'], ['edited title'], [False]),
    (['B'], ['A', 'B'], [True, False]),
    (['A', 'B'], ['B', 'C'], [False, True]),
])
def test_new_cross_source_shared_urls_use_previous_pairs_and_urls(old_titles, new_titles, expected):
    url = 'https://example.test/shared'
    previous = bundle([item(title, source='hn_week_top', url=url) for title in old_titles])
    current = bundle([item(title, source='hn_front_page', url=url) for title in new_titles])
    sweep.mark_new(current, previous)
    assert [e['new'] for e in current['items']] == expected
