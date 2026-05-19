"""NVIDIA NIM adapter for bounded market-state review.

Long backtests should use deterministic or cached reviews. This adapter is for
single-candle review, offline cache generation, or testnet/live decision checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any

import requests
from requests import RequestException

from src.gridbot.strategy.market_state import MarketStateDecision


@dataclass(frozen=True)
class NimReview:
    playbook: str
    risk_mode: str
    confidence: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AiRiskReview:
    decision: str
    risk_level: str
    risk_scale: float
    confidence: float
    reason_codes: tuple[str, ...]


class NimMarketReviewer:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        api_key_env: str = "NVIDIA_NIM_API_KEY",
        base_url_env: str = "NVIDIA_NIM_BASE_URL",
        model_env: str = "NVIDIA_NIM_MODEL",
        default_base_url: str = "https://integrate.api.nvidia.com/v1",
        default_model: str = "minimaxai/minimax-m2.7",
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self.base_url = (base_url or os.environ.get(base_url_env) or default_base_url).rstrip("/")
        self.model = model or os.environ.get(model_env, default_model)
        self.timeout = timeout

    def review(self, decision: MarketStateDecision, candidate: dict[str, Any] | None = None) -> NimReview:
        if not self.api_key:
            raise ValueError("NVIDIA_NIM_API_KEY is required for NimMarketReviewer")

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded crypto market-state reviewer. "
                        "Return only compact JSON with keys playbook, risk_mode, confidence, reason_codes. "
                        "Allowed playbook values: long_breakout, long_pullback, short_breakdown, vwap_reversion, no_trade. "
                        "Allowed risk_mode values: off, small, normal, aggressive. "
                        "The trade candidate already passed a deterministic strategy engine. "
                        "Your job is position risk scaling, not to reject most trades. "
                        "Use off only for hard invalid setups such as extremely thin volume, direct contradiction, or obvious fakeout. "
                        "Prefer small or normal when the setup is imperfect but tradable. "
                        "Do not invent data. Use only the provided market_state and candidate."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"market_state": _decision_payload(decision), "candidate": candidate or {}},
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        response = requests.post(
            _chat_completions_url(self.base_url),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content", "")
        return _parse_review(content)


class NimRiskJudge:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        api_key_env: str = "NVIDIA_NIM_API_KEY",
        base_url_env: str = "NVIDIA_NIM_BASE_URL",
        model_env: str = "NVIDIA_NIM_MODEL",
        default_base_url: str = "https://integrate.api.nvidia.com/v1",
        default_model: str = "minimaxai/minimax-m2.7",
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self.base_url = (base_url or os.environ.get(base_url_env) or default_base_url).rstrip("/")
        self.model = model or os.environ.get(model_env, default_model)
        self.timeout = timeout

    def review(self, decision: MarketStateDecision, candidate: dict[str, Any]) -> AiRiskReview:
        if not self.api_key:
            raise ValueError("NVIDIA_NIM_API_KEY is required for NimRiskJudge")

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded crypto risk judge for an already-generated ETHUSDC futures signal. "
                        "Return only compact JSON with keys decision, risk_level, risk_scale, confidence, reason_codes. "
                        "Allowed decision values: accept, reduce, reject. "
                        "Allowed risk_level values: low, medium, high, extreme. "
                        "risk_scale must be between 0 and 1. Use 1 for accept, 0.25/0.5 for reduce, and 0 for reject. "
                        "Your job is tail-risk control, not finding new trades. "
                        "The deterministic strategy intentionally uses aggressive position sizing on selected high-conviction signals; "
                        "do not reduce or reject just because leverage_cap, notional, margin, or allocated_risk_pct is high. "
                        "Position size may amplify an existing market flaw, but it is not by itself a market flaw. "
                        "Reduce only when size is combined with pre-entry evidence of weak volume, fake breakout/breakdown, "
                        "exhaustion, deep pullback contradiction, MA20/VWAP contradiction, or range/chop mismatch. "
                        "Reject only for extreme contradiction or obvious trap setups. "
                        "Accept strong aligned trend signals even when sizing is aggressive. "
                        "Do not invent data. Use only market_state and candidate."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"market_state": _decision_payload(decision), "candidate": candidate},
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        response = requests.post(
            _chat_completions_url(self.base_url),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        content = message.get("content") or message.get("reasoning_content", "")
        return _parse_risk_review(content)


class CachedNimMarketReviewer:
    def __init__(
        self,
        reviewer: NimMarketReviewer,
        cache_path: str | os.PathLike[str],
        fail_open: bool = True,
        cache_only: bool = False,
        fallback_reviewer: NimMarketReviewer | None = None,
    ) -> None:
        self.reviewer = reviewer
        self.cache_path = Path(cache_path)
        self.cache: dict[str, dict[str, Any]] = _load_cache(self.cache_path)
        self.fail_open = fail_open
        self.cache_only = cache_only
        self.fallback_reviewer = fallback_reviewer

    def review(
        self,
        decision: MarketStateDecision,
        cache_key: str,
        candidate: dict[str, Any] | None = None,
    ) -> NimReview:
        cached = self.cache.get(cache_key)
        if cached is not None:
            return NimReview(
                playbook=str(cached.get("playbook", "no_trade")),
                risk_mode=str(cached.get("risk_mode", "off")),
                confidence=float(cached.get("confidence", 0.0)),
                reason_codes=tuple(str(item) for item in cached.get("reason_codes", [])),
            )
        if self.cache_only:
            return NimReview(
                playbook=getattr(decision, "playbook", "no_trade"),
                risk_mode="small",
                confidence=0.5,
                reason_codes=("nim_cache_miss_fail_open",),
            )
        try:
            review = self.reviewer.review(decision, candidate=candidate)
        except (RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
            if self.fallback_reviewer is not None:
                try:
                    review = self.fallback_reviewer.review(decision, candidate=candidate)
                    review = NimReview(
                        playbook=review.playbook,
                        risk_mode=review.risk_mode,
                        confidence=review.confidence,
                        reason_codes=("fallback_minimax",) + tuple(review.reason_codes),
                    )
                    self.cache[cache_key] = {
                        "playbook": review.playbook,
                        "risk_mode": review.risk_mode,
                        "confidence": review.confidence,
                        "reason_codes": list(review.reason_codes),
                    }
                    _save_cache(self.cache_path, self.cache)
                    return review
                except (RequestException, ValueError, KeyError, json.JSONDecodeError):
                    pass
            if not self.fail_open:
                raise
            review = NimReview(
                playbook=getattr(decision, "playbook", "no_trade"),
                risk_mode="normal",
                confidence=0.5,
                reason_codes=(f"nim_error_fail_open:{exc.__class__.__name__}",),
            )
        self.cache[cache_key] = {
            "playbook": review.playbook,
            "risk_mode": review.risk_mode,
            "confidence": review.confidence,
            "reason_codes": list(review.reason_codes),
        }
        _save_cache(self.cache_path, self.cache)
        return review


class CachedNimRiskJudge:
    def __init__(
        self,
        reviewer: NimRiskJudge,
        cache_path: str | os.PathLike[str],
        fail_open: bool = True,
        cache_only: bool = False,
        fallback_reviewer: NimRiskJudge | None = None,
    ) -> None:
        self.reviewer = reviewer
        self.cache_path = Path(cache_path)
        self.cache: dict[str, dict[str, Any]] = _load_cache(self.cache_path)
        self.fail_open = fail_open
        self.cache_only = cache_only
        self.fallback_reviewer = fallback_reviewer

    def review(
        self,
        decision: MarketStateDecision,
        cache_key: str,
        candidate: dict[str, Any],
    ) -> AiRiskReview:
        cached = self.cache.get(cache_key)
        if cached is not None:
            return AiRiskReview(
                decision=str(cached.get("decision", "accept")),
                risk_level=str(cached.get("risk_level", "low")),
                risk_scale=float(cached.get("risk_scale", 1.0)),
                confidence=float(cached.get("confidence", 0.0)),
                reason_codes=tuple(str(item) for item in cached.get("reason_codes", [])),
            )
        if self.cache_only:
            return AiRiskReview("accept", "medium", 1.0, 0.5, ("ai_risk_cache_miss_fail_open",))
        try:
            review = self.reviewer.review(decision, candidate)
        except (RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
            if self.fallback_reviewer is not None:
                try:
                    review = self.fallback_reviewer.review(decision, candidate)
                    review = AiRiskReview(
                        review.decision,
                        review.risk_level,
                        review.risk_scale,
                        review.confidence,
                        ("fallback_minimax",) + tuple(review.reason_codes),
                    )
                    self.cache[cache_key] = _risk_review_payload(review)
                    _save_cache(self.cache_path, self.cache)
                    return review
                except (RequestException, ValueError, KeyError, json.JSONDecodeError):
                    pass
            if not self.fail_open:
                raise
            review = AiRiskReview("accept", "medium", 1.0, 0.5, (f"ai_risk_error_fail_open:{exc.__class__.__name__}",))
        self.cache[cache_key] = _risk_review_payload(review)
        _save_cache(self.cache_path, self.cache)
        return review


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _decision_payload(decision: MarketStateDecision) -> dict[str, Any]:
    payload = asdict(decision)
    payload["features"] = {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in payload["features"].items()
    }
    return payload


def _parse_review(content: str) -> NimReview:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("NIM review did not return JSON")
    data = json.loads(content[start:end + 1])
    playbook = data.get("playbook", "no_trade")
    risk_mode = data.get("risk_mode", "off")
    confidence = float(data.get("confidence", 0.0))
    reason_codes = tuple(str(item) for item in data.get("reason_codes", []))
    if playbook not in {"long_breakout", "long_pullback", "short_breakdown", "vwap_reversion", "no_trade"}:
        playbook = "no_trade"
    if risk_mode not in {"off", "small", "normal", "aggressive"}:
        risk_mode = "off"
    return NimReview(
        playbook=playbook,
        risk_mode=risk_mode,
        confidence=max(0.0, min(confidence, 1.0)),
        reason_codes=reason_codes,
    )


def _parse_risk_review(content: str) -> AiRiskReview:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("NIM risk review did not return JSON")
    data = json.loads(content[start:end + 1])
    decision = data.get("decision", "accept")
    risk_level = data.get("risk_level", "medium")
    risk_scale = float(data.get("risk_scale", 1.0))
    confidence = float(data.get("confidence", 0.0))
    reason_codes = tuple(str(item) for item in data.get("reason_codes", []))
    if decision not in {"accept", "reduce", "reject"}:
        decision = "reject"
    if risk_level not in {"low", "medium", "high", "extreme"}:
        risk_level = "high"
    if decision == "reject":
        risk_scale = 0.0
    elif decision == "accept":
        risk_scale = 1.0
    return AiRiskReview(
        decision=decision,
        risk_level=risk_level,
        risk_scale=max(0.0, min(risk_scale, 1.0)),
        confidence=max(0.0, min(confidence, 1.0)),
        reason_codes=reason_codes,
    )


def _risk_review_payload(review: AiRiskReview) -> dict[str, Any]:
    return {
        "decision": review.decision,
        "risk_level": review.risk_level,
        "risk_scale": review.risk_scale,
        "confidence": review.confidence,
        "reason_codes": list(review.reason_codes),
    }


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
