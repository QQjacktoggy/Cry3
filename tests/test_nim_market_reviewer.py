import unittest
import tempfile
from pathlib import Path

from src.gridbot.strategy.nim_market_reviewer import (
    AiRiskReview,
    CachedNimMarketReviewer,
    CachedNimRiskJudge,
    NimReview,
    _chat_completions_url,
    _parse_review,
    _parse_risk_review,
)


class _FakeReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, decision, candidate=None):
        self.calls += 1
        return NimReview("long_breakout", "normal", 0.7, ("cached",))


class _FailingReviewer:
    def review(self, decision, candidate=None):
        raise ValueError("boom")


class _FallbackReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, decision, candidate=None):
        self.calls += 1
        return NimReview("long_pullback", "normal", 0.66, ("ok",))


class _FakeRiskJudge:
    def __init__(self):
        self.calls = 0

    def review(self, decision, candidate):
        self.calls += 1
        return AiRiskReview("reduce", "high", 0.35, 0.74, ("tail_risk",))


class TestNimMarketReviewer(unittest.TestCase):
    def test_parse_review_accepts_json_only_response(self):
        review = _parse_review(
            '{"playbook":"long_breakout","risk_mode":"normal","confidence":0.82,"reason_codes":["bull_n"]}'
        )

        self.assertEqual(review.playbook, "long_breakout")
        self.assertEqual(review.risk_mode, "normal")
        self.assertEqual(review.confidence, 0.82)
        self.assertEqual(review.reason_codes, ("bull_n",))

    def test_parse_review_accepts_json_inside_reasoning_text(self):
        review = _parse_review(
            'Thinking... final answer: {"playbook":"no_trade","risk_mode":"off","confidence":0.61,"reason_codes":["thin_volume"]}'
        )

        self.assertEqual(review.playbook, "no_trade")
        self.assertEqual(review.risk_mode, "off")
        self.assertEqual(review.reason_codes, ("thin_volume",))

    def test_parse_review_clamps_invalid_values_to_safe_defaults(self):
        review = _parse_review(
            '{"playbook":"ape_in","risk_mode":"max","confidence":4.2,"reason_codes":["bad"]}'
        )

        self.assertEqual(review.playbook, "no_trade")
        self.assertEqual(review.risk_mode, "off")
        self.assertEqual(review.confidence, 1.0)

    def test_chat_completions_url_accepts_openai_style_base_url(self):
        self.assertEqual(
            _chat_completions_url("https://integrate.api.nvidia.com/v1"),
            "https://integrate.api.nvidia.com/v1/chat/completions",
        )
        self.assertEqual(
            _chat_completions_url("https://integrate.api.nvidia.com"),
            "https://integrate.api.nvidia.com/v1/chat/completions",
        )

    def test_cached_reviewer_reuses_cached_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _FakeReviewer()
            reviewer = CachedNimMarketReviewer(fake, Path(tmpdir) / "nim_cache.json")

            first = reviewer.review(object(), "k1")
            second = reviewer.review(object(), "k1")

        self.assertEqual(first, second)
        self.assertEqual(fake.calls, 1)

    def test_cached_reviewer_fail_open_returns_neutral_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reviewer = CachedNimMarketReviewer(_FailingReviewer(), Path(tmpdir) / "nim_cache.json")

            review = reviewer.review(object(), "k1")

        self.assertEqual(review.risk_mode, "normal")
        self.assertEqual(review.confidence, 0.5)
        self.assertTrue(review.reason_codes[0].startswith("nim_error_fail_open"))

    def test_cached_reviewer_cache_only_does_not_call_api_on_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _FakeReviewer()
            reviewer = CachedNimMarketReviewer(fake, Path(tmpdir) / "nim_cache.json", cache_only=True)

            review = reviewer.review(object(), "k1")

        self.assertEqual(review.risk_mode, "small")
        self.assertEqual(fake.calls, 0)

    def test_cached_reviewer_uses_fallback_before_fail_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback = _FallbackReviewer()
            reviewer = CachedNimMarketReviewer(
                _FailingReviewer(),
                Path(tmpdir) / "nim_cache.json",
                fallback_reviewer=fallback,
            )

            review = reviewer.review(object(), "k1")

        self.assertEqual(review.playbook, "long_pullback")
        self.assertEqual(review.risk_mode, "normal")
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(review.reason_codes[0], "fallback_minimax")

    def test_parse_risk_review_accepts_bounded_decision(self):
        review = _parse_risk_review(
            '{"decision":"reduce","risk_level":"high","risk_scale":0.25,"confidence":0.82,"reason_codes":["overextended"]}'
        )

        self.assertEqual(review.decision, "reduce")
        self.assertEqual(review.risk_level, "high")
        self.assertEqual(review.risk_scale, 0.25)
        self.assertEqual(review.reason_codes, ("overextended",))

    def test_parse_risk_review_reject_forces_zero_scale(self):
        review = _parse_risk_review(
            '{"decision":"reject","risk_level":"extreme","risk_scale":1,"confidence":0.91,"reason_codes":["fake_breakdown"]}'
        )

        self.assertEqual(review.decision, "reject")
        self.assertEqual(review.risk_scale, 0.0)

    def test_cached_risk_judge_reuses_cached_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _FakeRiskJudge()
            reviewer = CachedNimRiskJudge(fake, Path(tmpdir) / "risk_cache.json")

            first = reviewer.review(object(), "k1", {"strategy": "orb_short"})
            second = reviewer.review(object(), "k1", {"strategy": "orb_short"})

        self.assertEqual(first, second)
        self.assertEqual(fake.calls, 1)


if __name__ == "__main__":
    unittest.main()
