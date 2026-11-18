import unittest
from unittest.mock import patch

from lib import cjk, dedupe, relevance


class _BigramBase(unittest.TestCase):
    """Force the dictionary-free bigram path so assertions are deterministic
    regardless of whether jieba is installed in the test environment."""

    def setUp(self):
        patcher = patch.object(cjk, "_jieba", None)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestCjkSegment(_BigramBase):
    def test_has_cjk(self):
        self.assertTrue(cjk.has_cjk("国产大模型"))
        self.assertTrue(cjk.has_cjk("GPT4很强"))
        self.assertFalse(cjk.has_cjk("hello world"))
        self.assertFalse(cjk.has_cjk(""))

    def test_ascii_path_unchanged(self):
        # Non-CJK text keeps whitespace/word tokenization.
        self.assertEqual(cjk.segment("best react hooks"), ["best", "react", "hooks"])

    def test_chinese_bigrams(self):
        toks = cjk.segment("大模型")
        self.assertIn("大模", toks)
        self.assertIn("模型", toks)

    def test_mixed_language(self):
        toks = cjk.segment("GPT4很强 react")
        self.assertIn("gpt4", toks)
        self.assertIn("react", toks)
        self.assertIn("很强", toks)

    def test_single_cjk_char(self):
        self.assertEqual(cjk.segment("中"), ["中"])


class TestChineseRelevance(_BigramBase):
    def test_chinese_query_matches_chinese_text(self):
        q = relevance.PreparedQuery("国产大模型 测评")
        score = relevance.token_overlap_relevance(q, "这是国产大模型的最新测评")
        self.assertGreater(score, 0.5)

    def test_chinese_query_rejects_unrelated_text(self):
        q = relevance.PreparedQuery("国产大模型 测评")
        score = relevance.token_overlap_relevance(q, "今天天气很好适合出门散步")
        self.assertEqual(score, 0.0)

    def test_english_relevance_not_regressed(self):
        q = relevance.PreparedQuery("react hooks")
        self.assertGreaterEqual(relevance.token_overlap_relevance(q, "a guide to react hooks"), 0.9)

    def test_cjk_phrase_bonus_applies_on_contiguous_match(self):
        # A multi-token CJK query ("国产大模型 测评") whose words appear
        # contiguously in the text earns the phrase bonus via the space-stripped
        # containment retry; the same words scattered apart do not.
        q = relevance.PreparedQuery("国产大模型 测评")
        contiguous = relevance.token_overlap_relevance(q, "国产大模型测评合集")
        scattered = relevance.token_overlap_relevance(q, "测评了很多东西也聊到国产大模型")
        self.assertGreater(contiguous, scattered)

    def test_english_phrase_bonus_stays_space_sensitive(self):
        q = relevance.PreparedQuery("react hooks")
        joined = relevance.token_overlap_relevance(q, "react myreacthooks bundle")
        absent = relevance.token_overlap_relevance(q, "react unrelated bundle")
        self.assertEqual(joined, 0.58)
        self.assertEqual(absent, 0.58)


class TestChineseDedupe(_BigramBase):
    def test_reordered_chinese_is_near_duplicate(self):
        sim = dedupe.hybrid_similarity("国产大模型最新测评对比", "国产大模型测评对比最新")
        self.assertGreater(sim, 0.5)

    def test_distinct_chinese_is_not_duplicate(self):
        sim = dedupe.hybrid_similarity("国产大模型测评", "今天天气很好出门散步")
        self.assertLess(sim, 0.3)


class _JiebaLike:
    """Segments the way jieba does without needing it installed: a known
    Chinese word comes back whole, and everything its dictionary does not
    cover (Hangul included) comes back one character at a time."""

    WORDS = ("大模型", "测评")

    def __init__(self):
        self.seen = []

    def cut(self, run):
        self.seen.append(run)
        pos = 0
        while pos < len(run):
            word = next((w for w in self.WORDS if run.startswith(w, pos)), run[pos])
            yield word
            pos += len(word)


class TestHangulWithJiebaInstalled(unittest.TestCase):
    """jieba emits Hangul syllable by syllable, and the tokenizers drop
    single-character tokens, so Korean must not be routed through it."""

    def setUp(self):
        self.jieba = _JiebaLike()
        patcher = patch.object(cjk, "_jieba", self.jieba)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_hangul_is_bigrammed_not_sent_to_jieba(self):
        self.assertEqual(cjk.segment("갤럭시 폴드"), ["갤럭", "럭시", "폴드"])
        self.assertEqual(self.jieba.seen, [])

    def test_hangul_segments_the_same_with_and_without_jieba(self):
        text = "삼성 갤럭시 폴드를 한 달 써 본 후기"
        with_jieba = cjk.segment(text)
        with patch.object(cjk, "_jieba", None):
            without_jieba = cjk.segment(text)
        self.assertEqual(with_jieba, without_jieba)

    def test_chinese_still_goes_to_jieba(self):
        self.assertEqual(cjk.segment("大模型测评"), ["大模型", "测评"])
        self.assertEqual(self.jieba.seen, ["大模型测评"])

    def test_a_run_mixing_hangul_and_chinese_splits_at_the_boundary(self):
        self.assertEqual(cjk.segment("大模型갤럭시测评"), ["大模型", "갤럭", "럭시", "测评"])
        self.assertEqual(self.jieba.seen, ["大模型", "测评"])

    def test_korean_relevance_separates_on_topic_from_off_topic(self):
        q = relevance.PreparedQuery("갤럭시 폴드 후기")
        self.assertGreater(relevance.token_overlap_relevance(q, "갤럭시 폴드를 한 달 써 본 솔직한 후기입니다"), 0.5)
        self.assertEqual(relevance.token_overlap_relevance(q, "오늘 점심은 김치찌개를 먹었습니다"), 0.0)

    def test_korean_near_duplicates_are_detected(self):
        self.assertGreater(
            dedupe.token_jaccard("삼성 갤럭시 폴드 신제품 후기 쏟아져", "삼성 갤럭시 폴드 신제품 후기 쏟아져 (종합)"),
            0.7,
        )
        self.assertEqual(dedupe.token_jaccard("삼성 갤럭시 폴드 후기", "오늘 점심 김치찌개 맛집"), 0.0)


class TestJiebaBinding(unittest.TestCase):
    def test_jieba_global_is_bound_at_import(self):
        # Eager import binds the module global once (None when jieba absent).
        # No lazy initializer => no per-call race in the pipeline thread pool.
        self.assertTrue(hasattr(cjk, "_jieba"))
        self.assertFalse(hasattr(cjk, "_get_jieba"))


if __name__ == "__main__":
    unittest.main()
