"""Synthetic YouTube API fixtures only; no captured API data or network calls."""

import io
import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from lib import env, http, trend_sweep as sweep, youtube_data as yt

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
FAKE_KEY = 'obviously-fake-youtube-api-key-for-tests'


def video(number, title='가상 로봇 만들기', channel='연습 채널', views='123', duration='PT25S'):
    return {'id': f'vid{number:08d}', 'snippet': {
        'title': title, 'channelTitle': channel, 'description': '<b>가상 설명</b> &amp; test',
        'publishedAt': '2026-11-19T10:30:00Z'},
        'statistics': {'viewCount': views, 'likeCount': '12', 'commentCount': '3'},
        'contentDetails': {'duration': duration}}


def synthetic_responses():
    videos = [video(1), video(2, 'Synthetic English', '가상 채널', '789', 'PT1M7S'),
              video(3, 'English only', 'Invented channel', '999', 'PT6H28M57S')]
    return [
        {'items': deepcopy(videos)},
        {'items': [{'id': {'kind': 'youtube#video', 'videoId': v['id']},
                    'snippet': deepcopy(v['snippet'])} for v in videos]},
        {'items': list(reversed(deepcopy(videos)))},
    ]


class Response(io.BytesIO):
    status = 200


def fake_response(payload):
    return Response(json.dumps(payload).encode())


def collected():
    with mock.patch.object(http, 'open_request', side_effect=[fake_response(v) for v in synthetic_responses()]):
        return yt.collect(FAKE_KEY, sweep.fetch_url, topics=['AI'], now=NOW)


def test_requests_use_header_honest_agent_single_bounded_transport():
    streams = [fake_response(v) for v in synthetic_responses()]
    reads = [mock.Mock(wraps=s.read) for s in streams]
    for stream, read in zip(streams, reads):
        stream.read = read
    with mock.patch.object(http, 'open_request', side_effect=streams) as opened:
        result = yt.collect(FAKE_KEY, sweep.fetch_url, topics=['AI'], now=NOW)
    assert result['quota'] == {'search_calls': 1, 'units': 2}
    assert opened.call_count == 3
    requests = [call.args[0] for call in opened.call_args_list]
    for req, read in zip(requests, reads):
        assert req.get_header('X-goog-api-key') == FAKE_KEY
        assert req.get_header('User-agent') == http.USER_AGENT
        assert FAKE_KEY not in req.full_url and 'key=' not in req.full_url
        read.assert_called_once_with(5 * 1024 * 1024 + 1)
        guard = getattr(req, http._REDIRECT_GUARD_ATTR)
        assert guard('https://www.googleapis.com/youtube/v3/videos')
        assert not guard('http://www.googleapis.com/youtube/v3/videos')
        assert not guard('https://example.test/steal-header')
    query = lambda req: parse_qs(urlsplit(req.full_url).query)
    assert query(requests[0]) == {'part': [yt.PARTS], 'chart': ['mostPopular'], 'regionCode': ['KR'], 'maxResults': ['50']}
    assert query(requests[1]) == {'part': ['snippet'], 'type': ['video'], 'q': ['AI'],
        'regionCode': ['KR'], 'relevanceLanguage': ['ko'], 'order': ['viewCount'],
        'publishedAfter': ['2026-11-13T00:00:00Z'], 'maxResults': ['50']}
    assert query(requests[2]) == {'part': [yt.PARTS], 'id': ['vid00000001,vid00000002,vid00000003']}


def test_merge_by_id_sort_views_and_filter_search_only():
    result = collected()
    chart, search = result['items'][:3], result['items'][3:]
    assert len(chart) == 3 and [i['extra']['has_hangul'] for i in chart] == [True, True, False]
    assert [i['url'] for i in search] == ['https://www.youtube.com/watch?v=vid00000002', 'https://www.youtube.com/watch?v=vid00000001']
    assert [i['metric']['value'] for i in search] == [789, 123]
    assert search[1]['extra'] == {'channel': '연습 채널', 'likes': 12, 'comments': 3,
        'duration_seconds': 25, 'short': True, 'topic': 'AI', 'has_hangul': True}
    assert search[0]['date'] == '2026-11-19'
    assert search[0]['summary'] == '가상 설명 & test'
    assert result['delete_by'] == '2026-12-20'


@pytest.mark.parametrize('raw,seconds', [('PT25S', 25), ('PT1M7S', 67), ('PT6H28M57S', 23337),
    ('PT60S', 60), ('PT0S', 0), ('P1DT2H', 93600), ('bad', None), ('PT', None), (None, None), (123, None)])
def test_duration(raw, seconds):
    assert yt.duration_seconds(raw) == seconds
    result = yt._item(video(1, duration=raw), 'youtube_chart_kr')
    assert result['extra']['short'] == (seconds <= 60 if seconds is not None else None)


def test_missing_statistics_are_unknown_not_zero_and_missing_video_keeps_search_snippet():
    chart, search, stats = synthetic_responses()
    stats['items'] = [{'id': 'vid00000001', 'statistics': {}}]
    fetch = mock.Mock(side_effect=[json.dumps(v) for v in (chart, search, stats)])
    result = yt.collect(FAKE_KEY, fetch, topics=['AI'], now=NOW)
    items = result['items'][3:]
    assert len(items) == 2
    for item in items:
        assert item['metric']['value'] is None
        assert all(item['extra'][k] is None for k in ('likes', 'comments', 'duration_seconds', 'short'))


def test_search_cap_and_default_topics():
    assert yt.DEFAULT_TOPICS == ('AI', '인공지능', '챗GPT', 'AI 에이전트', '자동화', '창업', '부업', '쇼츠', '밈', '챌린지', '신상', '리뷰')
    chart, search, stats = synthetic_responses()
    fetch = mock.Mock(side_effect=[json.dumps(chart), json.dumps(search), json.dumps(stats)])
    result = yt.collect(FAKE_KEY, fetch, searches=1, now=NOW)
    assert result['unchecked_topics'] == list(yt.DEFAULT_TOPICS[1:])
    assert fetch.call_count == 3
    assert result['quota'] == {'search_calls': 1, 'units': 2}
    fetch = mock.Mock(return_value=json.dumps(chart))
    result = yt.collect(FAKE_KEY, fetch, searches=0, now=NOW)
    fetch.assert_called_once()
    assert result['unchecked_topics'] == list(yt.DEFAULT_TOPICS)


@pytest.mark.parametrize('fail_at,expected_calls,unchecked', [(0, 1, ['A', 'B']), (1, 2, ['A', 'B']), (2, 3, ['A', 'B'])])
def test_quota_403_stops_all_remaining_calls(fail_at, expected_calls, unchecked):
    responses = [fake_response(v) for v in synthetic_responses()]
    failure = HTTPError('https://www.googleapis.com/?key=' + FAKE_KEY, 403,
                        'quota exceeded ' + FAKE_KEY, {},
                        io.BytesIO(b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'))
    calls = responses[:fail_at] + [failure]
    with mock.patch.object(http, 'open_request', side_effect=calls) as opened:
        result = yt.collect(FAKE_KEY, sweep.fetch_url, topics=['A', 'B'], now=NOW)
    assert opened.call_count == expected_calls
    assert result['quota_stopped'] and result['errors']
    assert result['stopped']
    assert result['unchecked_topics'] == unchecked
    assert FAKE_KEY not in json.dumps(result)
    assert any('403' in e for e in result['errors'].values())


def test_quota_reason_in_body_stops_even_with_generic_403_message():
    error = HTTPError('https://www.googleapis.com/', 403, 'Forbidden', {},
                      io.BytesIO(b'{"error":{"errors":[{"reason":"dailyLimitExceeded"}]}}'))
    with mock.patch.object(http, 'open_request', side_effect=error) as opened:
        result = yt.collect(FAKE_KEY, sweep.fetch_url, now=NOW)
    opened.assert_called_once()
    assert result['quota_stopped']


@pytest.mark.parametrize('error', [TimeoutError('timeout ' + FAKE_KEY),
    sweep.FetchError('HTTP 500 server error ' + FAKE_KEY, 500), ValueError('bad JSON ' + FAKE_KEY)])
def test_other_failure_is_not_retried_and_next_topic_continues(error):
    fetch = mock.Mock(side_effect=['{"items":[]}', error, '{"items":[]}'])
    result = yt.collect(FAKE_KEY, fetch, topics=['A', 'B'], now=NOW)
    assert fetch.call_count == 3
    assert not result['quota_stopped']
    assert not result['stopped']
    assert result['unchecked_topics'] == ['A']
    assert FAKE_KEY not in json.dumps(result)
    assert result['quota'] == {'search_calls': 2, 'units': 1}


def test_payload_echoed_key_is_redacted_and_summary_is_bounded():
    payload = {'items': [video(1, title=FAKE_KEY, channel=FAKE_KEY)]}
    payload['items'][0]['snippet']['description'] = FAKE_KEY * 20
    result = yt.collect(FAKE_KEY, mock.Mock(return_value=json.dumps(payload)), topics=[], now=NOW)
    assert FAKE_KEY not in json.dumps(result)
    assert len(result['items'][0]['summary']) <= 200


def test_fifty_id_batch_without_pagination():
    hits = [{'id': {'videoId': f'vid{i:08d}'}, 'snippet': {'title': '가상 영상'}} for i in range(60)]
    fetch = mock.Mock(side_effect=['{"items":[]}', json.dumps({'items': hits, 'nextPageToken': 'ignored'}), '{"items":[]}'])
    result = yt.collect(FAKE_KEY, fetch, topics=['AI'], now=NOW)
    assert len(result['items']) == 50
    assert len(parse_qs(urlsplit(fetch.call_args.args[0]).query)['id'][0].split(',')) == 50
    assert fetch.call_count == 3


def test_youtube_transport_size_and_https_guards():
    with mock.patch.object(http, 'open_request', return_value=Response(b'x' * (sweep.MAX_BODY_BYTES + 1))) as opened:
        result = yt.collect(FAKE_KEY, sweep.fetch_url, topics=[], now=NOW)
    opened.assert_called_once()
    assert '5 MB' in result['errors']['youtube_chart_kr']
    with mock.patch.object(http, 'open_request') as opened, pytest.raises(ValueError, match='HTTPS'):
        sweep.fetch_url('http://example.test', extra_headers={'x-goog-api-key': FAKE_KEY})
    opened.assert_not_called()


def test_env_registers_youtube_key_without_credential_store_registration(monkeypatch):
    monkeypatch.setenv('YOUTUBE_API_KEY', FAKE_KEY)
    assert env.get_config()['YOUTUBE_API_KEY'] == FAKE_KEY
    assert 'YOUTUBE_API_KEY' not in env.KEYCHAIN_KEYS


def test_url_encoded_key_echoes_are_scrubbed():
    from urllib.parse import quote, quote_plus
    key = 'obviously-fake-key/+with spaces?='
    message = f'failed {key} https://example.test/?key={quote(key, safe="")} {quote_plus(key)}'
    result = yt.collect(key, mock.Mock(side_effect=ValueError(message)), topics=[], now=NOW)
    encoded = json.dumps(result, ensure_ascii=False)
    assert key not in encoded and quote(key, safe='') not in encoded and quote_plus(key) not in encoded
    assert '[REDACTED]' in encoded


def test_env_youtube_key_from_config_file():
    env.CONFIG_FILE.write_text(f'YOUTUBE_API_KEY={FAKE_KEY}\n')
    assert env.get_config()['YOUTUBE_API_KEY'] == FAKE_KEY


@pytest.mark.parametrize('code', [400, 401, 403])
@pytest.mark.parametrize('fail_at', [0, 1, 2], ids=['chart', 'search', 'statistics'])
def test_http_client_errors_stop_all_remaining_youtube_requests(code, fail_at):
    responses = [fake_response(v) for v in synthetic_responses()]
    failure = HTTPError('https://www.googleapis.com/youtube/v3/videos', code,
                        'invalid credentials ' + FAKE_KEY, {}, io.BytesIO(b''))
    with mock.patch.object(http, 'open_request', side_effect=responses[:fail_at] + [failure]) as opened:
        result = yt.collect(FAKE_KEY, sweep.fetch_url, topics=['A', 'B'], now=NOW)
    assert opened.call_count == fail_at + 1
    assert result['stopped'] is True
    assert result['quota_stopped'] is False
    assert result['unchecked_topics'] == ['A', 'B']
    section = 'youtube_chart_kr' if fail_at == 0 else 'youtube_search_kr: A'
    assert list(result['errors']) == [section]
    assert str(code) in result['errors'][section]
    assert FAKE_KEY not in json.dumps(result)
    assert result['quota'] == {'search_calls': int(fail_at > 0), 'units': 1 + int(fail_at == 2)}
