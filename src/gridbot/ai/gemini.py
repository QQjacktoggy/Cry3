"""Gemini AI client — interfaces with Google's Gemini API.

Uses google-genai SDK with response_schema for structured JSON output.
Includes retry logic for rate limit (429) errors.
"""

import asyncio
import re

from google import genai
from google.genai import types
from google.genai.errors import ClientError

from config.settings import Settings
from config.strategies import validate_and_clamp_adjustments
from src.gridbot.ai.models import GeminiRecommendation
from src.gridbot.ai.prompts import build_system_prompt, build_user_prompt
from src.gridbot.binance.models import MarketSnapshot, PositionInfo
from src.gridbot.grid.models import GridMetrics
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


class GeminiAnalyzer:
    """Gemini AI client for grid strategy analysis."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: genai.Client | None = None
        self._system_prompt = build_system_prompt()

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            if not self._settings.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY not configured")
            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        return self._client

    async def analyze(
        self,
        metrics: dict[str, GridMetrics],
        markets: dict[str, MarketSnapshot],
        positions: dict[str, PositionInfo | None],
        funding_rates: dict[str, list[dict]],
        current_strategy: str,
        account_balance: float | None = None,
        margin_ratio: float | None = None,
    ) -> GeminiRecommendation:
        """Run a complete analysis cycle and return structured recommendation.

        Args:
            metrics: Per-symbol grid metrics.
            markets: Per-symbol market snapshots.
            positions: Per-symbol position info.
            funding_rates: Per-symbol funding rate history.
            current_strategy: Currently active strategy name.
            account_balance: Total account balance.
            margin_ratio: Current margin ratio.

        Returns:
            GeminiRecommendation with validated parameters.
        """
        client = self._ensure_client()

        user_prompt = build_user_prompt(
            metrics=metrics,
            markets=markets,
            positions=positions,
            funding_rates=funding_rates,
            current_strategy=current_strategy,
            account_balance=account_balance,
            margin_ratio=margin_ratio,
        )

        logger.info(
            "gemini_analysis_started",
            model=self._settings.gemini_model,
            symbols=list(metrics.keys()),
            strategy=current_strategy,
        )

        try:
            response = await self._call_with_retry(
                user_prompt, structured=True
            )

            # Parse the structured response
            recommendation = GeminiRecommendation.model_validate_json(response)

            # Validate and clamp parameters against strategy bounds
            recommendation = self._validate_recommendation(recommendation)

            logger.info(
                "gemini_analysis_complete",
                strategy=recommendation.recommended_strategy,
                confidence=recommendation.confidence,
                leverage=recommendation.leverage_suggestion,
                direction=recommendation.direction_suggestion,
                warnings=len(recommendation.risk_warnings),
            )

            return recommendation

        except Exception as exc:
            logger.error("gemini_analysis_failed", error=str(exc))
            raise

    async def _call_with_retry(
        self,
        prompt: str,
        structured: bool = True,
        max_retries: int = 3,
    ) -> str:
        """Call Gemini API with retry on 429 rate limit errors."""
        client = self._ensure_client()

        for attempt in range(max_retries + 1):
            try:
                config = types.GenerateContentConfig(
                    system_instruction=self._system_prompt,
                    temperature=0.3,
                    max_output_tokens=4096,
                )
                if structured:
                    config.response_mime_type = "application/json"
                    config.response_schema = GeminiRecommendation

                response = await client.aio.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=prompt,
                    config=config,
                )
                return response.text

            except ClientError as exc:
                if exc.code == 429 and attempt < max_retries:
                    # Parse retry delay from error message
                    delay = self._parse_retry_delay(str(exc))
                    logger.warning(
                        "gemini_rate_limited",
                        attempt=attempt + 1,
                        retry_in=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    @staticmethod
    def _parse_retry_delay(error_msg: str) -> float:
        """Extract retry delay from Gemini 429 error message."""
        match = re.search(r"retry in (\d+\.?\d*)s", error_msg, re.IGNORECASE)
        if match:
            return float(match.group(1)) + 1.0  # add 1s buffer
        return 15.0  # default 15s

    def _validate_recommendation(self, rec: GeminiRecommendation) -> GeminiRecommendation:
        """Validate that recommendation parameters are within strategy bounds."""
        # Build adjustments dict from the recommendation
        adjustments: dict[str, float | int] = {}
        for adj in rec.parameter_adjustments:
            if isinstance(adj.suggested_value, (int, float)):
                adjustments[adj.parameter] = adj.suggested_value

        # Always include leverage and direction
        adjustments["leverage"] = rec.leverage_suggestion
        adjustments["direction"] = rec.direction_suggestion

        # Validate and clamp
        clamped, warnings = validate_and_clamp_adjustments(
            rec.recommended_strategy, adjustments
        )

        if warnings:
            logger.warning("parameters_clamped", warnings=warnings)
            rec.risk_warnings.extend(warnings)

        # Update the recommendation with clamped values
        if "leverage" in clamped:
            rec.leverage_suggestion = int(clamped["leverage"])
        if "direction" in clamped:
            rec.direction_suggestion = clamped["direction"]

        # Update individual parameter adjustments
        for adj in rec.parameter_adjustments:
            if adj.parameter in clamped and isinstance(clamped[adj.parameter], (int, float)):
                adj.suggested_value = clamped[adj.parameter]

        return rec

    async def ask(
        self,
        question: str,
        context: str = "",
    ) -> str:
        """Free-form question to Gemini (for /ask command).

        Returns plain text response (not structured).
        """
        client = self._ensure_client()

        prompt = question
        if context:
            prompt = f"背景資料：\n{context}\n\n問題：{question}"

        try:
            response = await client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "你是一個加密貨幣合約網格交易顧問。以繁體中文回答問題。"
                        "回答要簡潔、專業、有建設性。不要做任何價格預測。"
                    ),
                    temperature=0.5,
                    max_output_tokens=1000,
                ),
            )
            return response.text

        except Exception as exc:
            logger.error("gemini_ask_failed", error=str(exc))
            return f"Gemini 分析失敗：{str(exc)}"
