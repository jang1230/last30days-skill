"""Offline format fixtures trimmed from probes/samples/<key>.txt (2026-11-20).

Fixtures live inline, like test_producthunt/test_geeknews. Only complete first
records and consumed fields remain. GitHub's truncated sample has its closing
article tag restored; it contains no stars-gained field (tested synthetically).
"""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import pytest

from lib import atom_read, trend_sources as sources

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
FIXTURES = {'producthunt': '<ns0:feed xmlns:ns0="http://www.w3.org/2005/Atom"><ns0:entry>\n'
                '    <ns0:id>tag:www.producthunt.com,2005:Post/1319697</ns0:id>\n'
                '    <ns0:published>2026-11-16T19:25:07-08:00</ns0:published>\n'
                '    <ns0:updated>2026-11-19T21:22:37-08:00</ns0:updated>\n'
                '    <ns0:link rel="alternate" type="text/html" '
                'href="https://www.producthunt.com/products/ofintech-desk" />\n'
                '    <ns0:title>OfinTech Desk</ns0:title>\n'
                '    <ns0:content type="html">&lt;p&gt;\n'
                '            Finance AI where every figure traces to its source\n'
                '          &lt;/p&gt;</ns0:content>\n'
                '    <ns0:author>\n'
                '      <ns0:name>Mohamed</ns0:name>\n'
                '    </ns0:author>\n'
                '  </ns0:entry></ns0:feed>',
 'geeknews': '<ns0:feed xmlns:ns0="http://www.w3.org/2005/Atom"><ns0:entry>\n'
             '  <ns0:title>Show GN: Namoo - 부모님의 이야기를 AI 인터뷰로 기록해 이북과 오디오북으로 만들어 주는 앱</ns0:title>\n'
             '  <ns0:link rel="alternate" type="text/html" '
             'href="https://news.hada.io/topic?id=37506&amp;utm_source=geeknews&amp;utm_medium=rss&amp;utm_campaign=news" '
             '/>\n'
             '  <ns0:id>https://news.hada.io/topic?id=37506</ns0:id>\n'
             '  <ns0:updated>2026-11-20T13:15:52+09:00</ns0:updated>\n'
             '  <ns0:published>2026-11-20T13:15:52+09:00</ns0:published>\n'
             '  <ns0:author>\n'
             '    <ns0:name>briancheong</ns0:name>\n'
             '    </ns0:author>\n'
             '  <ns0:content type="html" xml:lang="ko">&lt;p&gt;부모님의 이야기를 좀 더 체계적으로 기록하기 위해 Namoo를 '
             '만들었습니다.&lt;/p&gt;</ns0:content>\n'
             '</ns0:entry></ns0:feed>',
 'techmeme': '<rss><channel><item>\n'
             "  <title>Asian tech stocks plunge as Anthropic's record IPO fails to stem an AI sell-off; "
             'SoftBank falls ~15%, TSMC falls 5%+, and Samsung and SK Hynix fall 6%+ (William '
             'Sandlund/Financial Times)</title>\n'
             '  <link>https://www.techmeme.com/261120/p1#a261120p1</link>\n'
             "  <description>William Sandlund / Financial Times : Asian tech stocks plunge as Anthropic's "
             'record IPO fails to stem an AI sell-off; SoftBank falls ~15%, TSMC falls 5%+, and Samsung and '
             "SK Hynix fall 6%+ — Region's </description>\n"
             '  <pubDate>Fri, 20 Nov 2026 00:45:01 -0500</pubDate>\n'
             '  <guid>https://www.techmeme.com/261120/p1#a261120p1</guid>\n'
             '</item></channel></rss>',
 'google_trends_kr': '<rss xmlns:ns0="https://trends.google.com/trending/rss"><channel><item>\n\t\t\t<title>빅뱅</title>\n\t\t\t<ns0:approx_traffic>200+</ns0:approx_traffic>\n\t\t\t<description />\n\t\t\t<link>https://trends.google.com/trending/rss?geo=KR</link>\n\t\t\t<pubDate>Thu, 19 Nov 2026 20:50:00 -0800</pubDate>\n\t\t\t<ns0:news_item>\n\t\t\t\t<ns0:news_item_title>빅뱅 출신 승리, 만삭 아내와 동반 입국 포착…양손에 캐리어·명품백 직접 챙겨</ns0:news_item_title>\n\t\t\t\t</ns0:news_item>\n\t\t\t<ns0:news_item>\n\t\t\t\t<ns0:news_item_title>빅뱅 승리, 만삭 아내와 공항서 포착…내년 1월 출산 예정</ns0:news_item_title>\n\t\t\t\t</ns0:news_item>\n\t\t\t<ns0:news_item>\n\t\t\t\t<ns0:news_item_title>승리, \'임신 8개월\' 아내와 동반 입국…D라인 포착</ns0:news_item_title>\n\t\t\t\t</ns0:news_item>\n\t\t</item><item>\n\t\t\t<title>빅뱅 신곡</title>\n\t\t\t<ns0:approx_traffic>200+</ns0:approx_traffic>\n\t\t\t<link>https://trends.google.com/trending/rss?geo=KR</link>\n\t\t\t<pubDate>Thu, 19 Nov 2026 20:50:00 -0800</pubDate>\n\t\t\t</item><item>\n\t\t\t<title>대상 공원 빅 트리</title>\n\t\t\t<ns0:approx_traffic>100+</ns0:approx_traffic>\n\t\t\t<link>https://trends.google.com/trending/rss?geo=KR</link>\n\t\t\t<pubDate>Thu, 19 Nov 2026 20:50:00 -0800</pubDate>\n\t\t\t</item></channel></rss>',
 'github_trending_weekly': '<article class="Box-row">\n'
                           '<h2 class="h3 lh-condensed">\n'
                           '    <a href="/backnotprop/plannotator" data-view-component="true" class="Link">\n'
                           '\n'
                           '      <span data-view-component="true" class="text-normal">\n'
                           '        backnotprop /\n'
                           '</span>\n'
                           '      plannotator</a>  </h2>\n'
                           '<p class="col-9 color-fg-muted my-1 tmp-pr-4">\n'
                           '      The workspace to stay in the loop and on top of agents - shape, decide, '
                           'plan, annotate, review code, share with team, and send all feedback to agents '
                           'with one click. \n'
                           '    </p>\n'
                           '<span itemprop="programmingLanguage">TypeScript</span>\n'
                           '</article>',
 'hn_front_page': '{"hits": [{"title": "Anthropic’s Nasdaq debut", "url": '
                  '"https://www.reuters.com/business/finance/anthropics-historic-debut-put-ais-promise-peril-wall-st-test-2026-11-19/", '
                  '"created_at": "2026-11-19T17:02:40Z", "points": 1026, "num_comments": 2025, "objectID": '
                  '"50562754"}]}',
 'huggingface_models': '[{"id": "higurashi-ai/Haru-4B", "likes": 3665, "pipeline_tag": "audio-to-audio", '
                       '"createdAt": "2026-11-12T11:05:59.000Z"}]',
 'huggingface_spaces': '[{"id": "huggingface/moe-blogpost", "likes": 754, "sdk": "docker", "createdAt": '
                       '"2026-10-09T15:24:25.000Z"}]',
 'lobsters': '[{"title": "Design for Understandability", "url": '
             '"https://jschuster.org/blog/2026/11/19/design-for-understandability/", "short_id_url": '
             '"https://lobste.rs/s/qsrnot", "created_at": "2026-11-19T07:07:34.542-06:00", "tags": ["api", '
             '"plt", "practices", "slides"], "score": 33, "comment_count": 20}]',
 'devto_top_week': '[{"title": "Is Coding with AI Actually Faster and Cheaper?", "url": '
                   '"https://dev.to/sylwia-lask/is-coding-with-ai-actually-faster-and-cheaper-2810", '
                   '"published_at": "2026-11-18T08:32:45Z", "description": "How has AI actually changed the '
                   'way we work? Are coding agents really making us remarkably faster?...", '
                   '"positive_reactions_count": 163, "comments_count": 143}]',
 'appstore_kr_top_free': '{"feed": {"results": [{"name": "네이버플러스 스토어", "url": '
                         '"https://apps.apple.com/kr/app/%EB%84%A4%EC%9D%B4%EB%B2%84%ED%94%8C%EB%9F%AC%EC%8A%A4-%EC%8A%A4%ED%86%A0%EC%96%B4/id6738063154", '
                         '"releaseDate": "2025-03-18", "artistName": "NAVER Corp."}]}}',
 'mastodon_trending_links': '[{"title": "Don\'t buy smart glasses, Dutch data watchdog advises", "url": '
                            '"https://www.bbc.co.uk/news/articles/ckq6eyrzrzy5o", "published_at": null, '
                            '"description": "Tests of glasses showed some misread medicine instructions or '
                            'pretended to have called an ambulance", "history": [{"day": "1795132800", '
                            '"accounts": "17", "uses": "17"}, {"day": "1795046400", "accounts": "232", '
                            '"uses": "230"}, {"day": "1794960000", "accounts": "0", "uses": "0"}, {"day": '
                            '"1794873600", "accounts": "0", "uses": "0"}, {"day": "1794787200", "accounts": '
                            '"0", "uses": "0"}, {"day": "1794700800", "accounts": "0", "uses": "0"}, {"day": '
                            '"1794614400", "accounts": "0", "uses": "0"}]}]',
 'mastodon_trending_tags': '[{"name": "hochwasser", "url": "https://mastodon.social/tags/hochwasser", '
                           '"history": [{"day": "1795132800", "accounts": "0", "uses": "0"}, {"day": '
                           '"1795046400", "accounts": "151", "uses": "152"}, {"day": "1794960000", '
                           '"accounts": "0", "uses": "0"}, {"day": "1794873600", "accounts": "22", "uses": '
                           '"22"}, {"day": "1794787200", "accounts": "8", "uses": "8"}, {"day": '
                           '"1794700800", "accounts": "2", "uses": "4"}, {"day": "1794614400", "accounts": '
                           '"0", "uses": "0"}]}]',
 'reddit_archive_sideproject': '{"data": [{"title": "I built an AI tool that turns any topic into an '
                               'interactive learning map 🗺️", "permalink": '
                               '"/r/SideProject/comments/1y0vpwg/i_built_an_ai_tool_that_turns_any_topic_into_an/", '
                               '"created_utc": 1795153962, "score": 1, "num_comments": 0, "selftext": '
                               '"[removed]"}]}',
 'huggingface_daily_papers': '[{"paper": {"id": "2611.26298", "title": "DailySafe: Benchmarking Dynamic '
                             'Safety Awareness in Everyday Multi-Turn Multimodal Interactions", '
                             '"publishedAt": "2026-11-19T00:00:00.000Z", "summary": "As multimodal large '
                             'language models (MLLMs) become increasingly integrated into everyday life, '
                             'their ability to provide safe and reliable assistance in realistic daily '
                             'activities remains insufficiently explored. Existing safety benchmarks p", '
                             '"upvotes": 1}, "publishedAt": "2026-11-18T19:00:00.000Z"}]',
 'wikipedia_en_top': '{"items": [{"project": "en.wikipedia", "year": "2026", "month": "11", "day": "18", '
                     '"articles": [{"article": "Main_Page", "views": 3817218, "rank": 1}, {"article": '
                     '"Special:Search", "views": 777700, "rank": 2}, {"article": '
                     '"Wikipedia:Featured_pictures", "views": 534251, "rank": 3}, {"article": '
                     '"Grand_Theft_Auto_VI", "views": 519632, "rank": 4}, {"article": "Ramayana:_Part_1", '
                     '"views": 410267, "rank": 5}, {"article": "Robert_Pattinson", "views": 337514, "rank": '
                     '6}, {"article": "Dancing_with_the_Stars_(American_TV_series)_season_35", "views": '
                     '150752, "rank": 7}]}]}',
 'wikipedia_ko_top': '{"items": [{"project": "ko.wikipedia", "year": "2026", "month": "11", "day": "18", '
                     '"articles": [{"article": "위키백과:대문", "views": 52124, "rank": 1}, {"article": "문화방송", '
                     '"views": 19140, "rank": 2}, {"article": "특수:검색", "views": 11001, "rank": 3}, '
                     '{"article": "한국방송공사", "views": 10745, "rank": 4}, {"article": "태종무열왕", "views": 4712, '
                     '"rank": 5}, {"article": "한국교육방송공사", "views": 4414, "rank": 6}, {"article": "문무왕", '
                     '"views": 2942, "rank": 7}]}]}'}


FORMAT_FIXTURES = {
    'feed': 'techmeme', 'producthunt': 'producthunt', 'geeknews': 'geeknews',
    'hn': 'hn_front_page', 'github': 'github_trending_weekly',
    'hf_models': 'huggingface_models', 'hf_spaces': 'huggingface_spaces',
    'hf_papers': 'huggingface_daily_papers', 'lobsters': 'lobsters', 'devto': 'devto_top_week',
    'wikipedia': 'wikipedia_en_top', 'apple': 'appstore_kr_top_free',
    'mastodon_links': 'mastodon_trending_links', 'mastodon_tags': 'mastodon_trending_tags',
    'reddit': 'reddit_archive_sideproject',
}


@pytest.mark.parametrize('format,title,url_fragment,date,label,value', [
    ('feed', 'Asian tech stocks plunge', '/261120/p1#a261120p1', '2026-11-20', '', None),
    ('producthunt', 'OfinTech Desk', '/products/ofintech-desk', '2026-11-17', '', None),
    ('geeknews', 'Show GN: Namoo', '/topic?id=37506', '2026-11-20', '', None),
    ('hn', 'Anthropic’s Nasdaq debut', 'reuters.com/business/finance/', '2026-11-19', 'points', 1026),
    ('github', 'backnotprop/plannotator', '/backnotprop/plannotator', '', 'stars gained', None),
    ('hf_models', 'higurashi-ai/Haru-4B', '/higurashi-ai/Haru-4B', '2026-11-12', 'likes', 3665),
    ('hf_spaces', 'huggingface/moe-blogpost', '/spaces/huggingface/moe-blogpost', '2026-10-09', 'likes', 754),
    ('hf_papers', 'DailySafe:', '/papers/2611.26298', '2026-11-18', 'upvotes', 1),
    ('lobsters', 'Design for Understandability', '/design-for-understandability/', '2026-11-19', 'score', 33),
    ('devto', 'Is Coding with AI', '/sylwia-lask/', '2026-11-18', 'reactions', 163),
    ('wikipedia', 'Grand Theft Auto VI', '/wiki/Grand_Theft_Auto_VI', '2026-11-18', 'views', 519632),
    ('apple', '네이버플러스 스토어', '/id6738063154', '', 'rank', 1),
    ('mastodon_links', "Don't buy smart glasses", '/news/articles/ckq6eyrzrzy5o', '2026-11-20', 'uses (7 days)', 247),
    ('mastodon_tags', 'hochwasser', '/tags/hochwasser', '2026-11-20', 'uses (7 days)', 186),
])
def test_format_samples(format, title, url_fragment, date, label, value):
    source = next(s for s in sources.SOURCES if s.format == format)
    item = sources.parse_source(source, FIXTURES[FORMAT_FIXTURES[format]])[0]
    assert item['title'].startswith(title)
    assert item['url'].startswith('https://') and url_fragment in item['url']
    assert item['date'] == date
    assert item['metric'] == {'label': label, 'value': value}
    assert item['source'] == source.id and item['group'] == source.group
    assert len(item['summary']) <= 200
    assert set(item) == {'source', 'group', 'title', 'summary', 'url', 'date', 'metric', 'extra'}


def test_registry_complete_and_valid():
    expected = '''producthunt hn_front_page hn_week_top hn_show github_trending_weekly
    github_trending_daily huggingface_models huggingface_spaces huggingface_daily_papers
    techmeme lobsters devto_top_week devto_ai_week techcrunch_ai techcrunch_startups
    google_trends_us wikipedia_en_top appstore_us_top_free mastodon_links mastodon_tags
    reddit_sideproject reddit_saas knowyourmeme_news knowyourmeme_memes geeknews yozm_it
    platum venturesquare byline_network aitimes etnews zdnet_korea bloter tech42 startuprecipe
    google_trends_kr wikipedia_ko_top appstore_kr_top_free applemusic_kr reddit_korea'''.split()
    assert len(sources.SOURCES) == len(expected) == len({s.id for s in sources.SOURCES})
    assert {s.id for s in sources.SOURCES} == set(expected)
    for source in sources.SOURCES:
        assert source.group in sources.GROUPS
        assert source.format in sources.PARSERS
        assert source.limit > 0
        url = sources.source_url(source, NOW)
        assert urlsplit(url).scheme == 'https' and urlsplit(url).hostname
        assert '{' not in url
        if source.format == 'hn':
            params = parse_qs(urlsplit(url).query)
            assert 'points' not in url
            assert params['hitsPerPage'] == [str(source.limit)]
            if source.id == 'hn_show':
                assert params['tags'] == ['show_hn']
                assert params['numericFilters'] == ['created_at_i>1794528000']
        if source.format == 'wikipedia':
            assert url.endswith('/2026/11/18')
    assert {s.id for s in sources.SOURCES if s.group == 'video'} == {'knowyourmeme_news', 'knowyourmeme_memes'}


def test_google_trends_fields():
    item = sources.parse_feed(FIXTURES['google_trends_kr'])[0]
    assert item['title'] == '빅뱅'
    assert item['date'] == '2026-11-20'
    assert item['metric'] == {'label': 'approx_traffic', 'value': '200+'}
    assert len(item['extra']['news_titles']) == 3
    assert '빅뱅 출신 승리' in item['summary']


@pytest.mark.parametrize('name', ['wikipedia_en_top', 'wikipedia_ko_top'])
def test_wikipedia_namespace_filter_and_limit(name):
    data = json.loads(FIXTURES[name])
    data['items'][0]['articles'] += [
        {'article': 'User:fake-user', 'views': 100, 'rank': 8},
        {'article': '특수:검색', 'views': 90, 'rank': 9},
        *[{'article': f'Article_{i}', 'views': i, 'rank': 10+i} for i in range(70)],
    ]
    source = next(s for s in sources.SOURCES if s.id == name)
    rows = sources.parse_source(source, json.dumps(data))
    assert len(rows) == 50
    assert all(not e['title'].startswith(('Main Page', 'Special:', 'Wikipedia:', '위키백과:', '특수:', 'User:')) for e in rows)
    if name == 'wikipedia_en_top':
        assert any(e['title'] == 'Ramayana: Part 1' for e in rows)
        assert rows[1]['url'].endswith('Ramayana%3A_Part_1')


def test_github_metric_and_structure_change():
    body = FIXTURES['github_trending_weekly'].replace('</article>', '<span>1,234 stars this week</span></article>')
    item = sources.parse_github(body)[0]
    assert item['metric'] == {'label': 'stars this week', 'value': 1234}
    assert item['extra']['language'] == 'TypeScript'
    for body in ('<html>new layout</html>', '<article class="Box-row">no heading</article>'):
        with pytest.raises(ValueError, match='structure changed'):
            sources.parse_github(body)


def test_hn_zero_points_and_url_fallback():
    data = json.loads(FIXTURES['hn_front_page'])
    data['hits'][0].update(points=0, url='')
    item = sources.parse_hn(json.dumps(data))[0]
    assert item['metric']['value'] == 0
    assert item['extra']['num_comments'] == 2025
    assert item['url'] == 'https://news.ycombinator.com/item?id=50562754'


def test_summary_cleans_markup_entities_and_caps_characters():
    item = sources.row('&lt;b&gt;Title&lt;/b&gt; &amp; text', 'https://example.test',
                       summary='<p>' + '한' * 300 + '</p>')
    assert item['title'] == 'Title & text'
    assert item['summary'] == '한' * 200


def test_atom_reuse_is_parsing_only():
    for name, module in [('producthunt', sources.producthunt), ('geeknews', sources.geeknews)]:
        with mock.patch.object(module, 'fetch_feed', side_effect=AssertionError('must not fetch')), \
             mock.patch.object(module, 'parse_feed', wraps=module.parse_feed) as parse:
            source = next(s for s in sources.SOURCES if s.id == name)
            assert sources.parse_source(source, FIXTURES[name])
            parse.assert_called_once()


@pytest.mark.parametrize('name', ['producthunt', 'geeknews', 'techmeme'])
def test_xml_parsers_accept_more_than_old_two_mb(name):
    source = next(s for s in sources.SOURCES if s.id == name)
    body = FIXTURES[name] + ' ' * (2 * 1024 * 1024)
    assert sources.parse_source(source, body)


def test_rss_and_atom_explicit_parse_cap():
    rss = FIXTURES['techmeme']
    assert atom_read.parse_entries(rss)[0]['link'] == 'https://www.techmeme.com/261120/p1#a261120p1'
    for body in (rss, FIXTURES['producthunt']):
        size = len(body.encode('utf-8'))
        assert atom_read.parse_entries(body, max_body_bytes=size)
        with pytest.raises(ValueError, match='exceeds'):
            atom_read.parse_entries(body, max_body_bytes=size-1)


@pytest.mark.parametrize('declaration', ['<!DOCTYPE rss>', '<!ENTITY x "bad">', '<!doctype rss>'])
def test_rss_rejects_declarations(declaration):
    with pytest.raises(ValueError, match='DOCTYPE|ENTITY'):
        sources.parse_feed(declaration + FIXTURES['techmeme'])


def test_apple_release_date_is_metadata_not_ranking_date():
    item = sources.parse_apple(FIXTURES['appstore_kr_top_free'])[0]
    assert item['date'] == ''
    assert item['extra']['release_date'] == '2025-03-18'
    assert item['metric'] == {'label': 'rank', 'value': 1}


def test_removed_reddit_sample_is_excluded():
    assert sources.parse_reddit(FIXTURES['reddit_archive_sideproject']) == []


def reddit_filter_fixture():
    # Synthetic variants of the real removed-post fixture exercise survivors/ties.
    template = json.loads(FIXTURES['reddit_archive_sideproject'])['data'][0]
    variants = [
        ('removed', 100, '[removed]', None),
        ('low', 1, '', None),
        ('deleted', 90, '[deleted]', None),
        ('newer-high', 50, 'Useful post', ''),
        ('moderated', 80, 'Still visible body', 'moderator'),
        ('older-high', 50, None, None),
        ('middle', 10, 'Another post', None),
    ]
    return json.dumps({'data': [dict(template, title=title, score=score, selftext=body,
                                   removed_by_category=reason, num_comments=i,
                                   permalink=f'/r/SideProject/comments/fake{i}/',
                                   created_utc=template['created_utc'] - i * 86400)
                               for i, (title, score, body, reason) in enumerate(variants)]})


def test_reddit_filters_removed_posts_and_sorts_scores_stably():
    items = sources.parse_reddit(reddit_filter_fixture())
    assert [e['title'] for e in items] == ['newer-high', 'older-high', 'middle', 'low']
    assert [e['metric'] for e in items] == [
        {'label': 'score', 'value': score} for score in [50, 50, 10, 1]]
    assert items[0]['url'] == 'https://www.reddit.com/r/SideProject/comments/fake3/'
    assert items[0]['date'] == '2026-11-17'
    assert items[0]['extra']['num_comments'] == 3
    assert items[0]['date'] > items[1]['date']


@pytest.mark.parametrize('value', [None, '', 'garbage', '2026-99-45', {}, [], True, False,
                                  float('nan'), float('inf'), -float('inf'), 10**100])
def test_unexpected_or_invalid_dates_are_empty(value):
    assert sources.day(value) == ''


def test_bad_feed_date_preserves_other_items():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(FIXTURES['techmeme'])
    channel = root.find('channel')
    original = channel.find('item')
    for title, date in [('bad date', 'not a date'), ('good date', 'Fri, 20 Nov 2026 01:00:00 +0000')]:
        node = ET.fromstring(ET.tostring(original, encoding='unicode'))
        node.find('title').text = title
        node.find('pubDate').text = date
        channel.append(node)
    rows = sources.parse_feed(ET.tostring(root, encoding='unicode'))
    assert len(rows) == 3
    assert [r['date'] for r in rows] == ['2026-11-20', '', '2026-11-20']


def test_item_urls_strip_utm_centrally_preserving_other_parameters():
    url = 'https://platum.kr/archives/123?keep=1&utm_source=rss&utm_medium=feed&UTM_campaign=launch&empty=#part'
    item = sources.row('Post', url)
    assert item['url'] == 'https://platum.kr/archives/123?keep=1&empty=#part'
    assert sources.row('Post', '')['url'] == ''
