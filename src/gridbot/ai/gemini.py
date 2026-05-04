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
from src.gridbot.ai.models import FinalGridParameters, GeminiRecommendation
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
        adjustments: dict[str, float | int | str] = {}
        for adj in rec.parameter_adjustments:
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

        # Ensure final grid parameters are always available for Telegram output
        rec.final_parameters = self._build_final_parameters(rec, adjustments)

        return rec

    @staticmethod
    def _get_adjustment_value(
        adjustments: dict[str, float | int | str],
        keys: tuple[str, ...],
    ) -> float | int | str | None:
        for key in keys:
            if key in adjustments:
                return adjustments[key]
        return None

    def _build_final_parameters(
        self,
        rec: GeminiRecommendation,
        adjustments: dict[str, float | int | str],
    ) -> FinalGridParameters:
        """Build explicit final parameters with robust fallback from adjustments."""
        fp = rec.final_parameters

        lower_price = self._get_adjustment_value(
            adjustments, ("grid_lower_price", "lower_price", "price_lower", "min_price")
        )
        upper_price = self._get_adjustment_value(
            adjustments, ("grid_upper_price", "upper_price", "price_upper", "max_price")
        )
        grid_count = self._get_adjustment_value(adjustments, ("num_grids", "grid_count"))
        grid_type = self._get_adjustment_value(adjustments, ("grid_type",))
        investment = self._get_adjustment_value(
            adjustments, ("investment_usdc", "invested_amount", "margin_amount_usdc")
        )
        margin_mode = self._get_adjustment_value(adjustments, ("margin_mode",))
        stop_loss = self._get_adjustment_value(
            adjustments, ("stop_loss_price", "stop_loss")
        )
        take_profit = self._get_adjustment_value(
            adjustments, ("take_profit_price", "take_profit")
        )
        close_all_on_stop = self._get_adjustment_value(
            adjustments, ("close_all_on_stop",)
        )

        return FinalGridParameters(
            symbol=fp.symbol,
            lower_price=fp.lower_price if fp.lower_price is not None else (
                float(lower_price) if isinstance(lower_price, (int, float)) else None
            ),
            upper_price=fp.upper_price if fp.upper_price is not None else (
                float(upper_price) if isinstance(upper_price, (int, float)) else None
            ),
            grid_count=fp.grid_count if fp.grid_count is not None else (
                int(grid_count) if isinstance(grid_count, (int, float)) else None
            ),
            grid_type=fp.grid_type if fp.grid_type is not None else (
                str(grid_type).upper() if str(grid_type).upper() in {"ARITHMETIC", "GEOMETRIC"} else None
            ),
            investment_usdc=fp.investment_usdc if fp.investment_usdc is not None else (
                float(investment) if isinstance(investment, (int, float)) else None
            ),
            leverage=rec.leverage_suggestion,
            direction=rec.direction_suggestion,
            margin_mode=fp.margin_mode if fp.margin_mode is not None else (
                str(margin_mode).upper() if str(margin_mode).upper() in {"CROSS", "ISOLATED"} else None
            ),
            stop_loss_price=fp.stop_loss_price if fp.stop_loss_price is not None else (
                float(stop_loss) if isinstance(stop_loss, (int, float)) else None
            ),
            take_profit_price=fp.take_profit_price if fp.take_profit_price is not None else (
                float(take_profit) if isinstance(take_profit, (int, float)) else None
            ),
            close_all_on_stop=fp.close_all_on_stop if fp.close_all_on_stop is not None else (
                bool(close_all_on_stop) if isinstance(close_all_on_stop, bool) else True
            ),
        )

    async def monitor_grid(
        self,
        symbol: str,
        market_price: float,
        lower_price: float,
        upper_price: float,
        grid_count: int,
        grid_type: str,
        leverage: int,
        direction: str,
        stop_loss_price: float | None,
        take_profit_price: float | None,
        invested_amount: float,
        session_start_ms: int,
        realized_pnl: float,
        funding_rate: float,
        funding_history: list[dict],
        klines: list,
    ) -> str:
        """30-minute grid monitoring analysis with Code Execution for technical indicators.

        Returns a plain-text Telegram message (no structured schema needed).
        """
        from datetime import datetime, timezone

        client = self._ensure_client()

        now = datetime.now(timezone.utc)
        session_start = datetime.fromtimestamp(session_start_ms / 1000, tz=timezone.utc)
        hours_running = (now - session_start).total_seconds() / 3600

        dist_upper_pct = (upper_price - market_price) / market_price * 100
        dist_lower_pct = (market_price - lower_price) / market_price * 100
        grid_type_label = "等比" if grid_type == "GEO" else "等差"
        fr_trend = "正值" if all(float(f.get("fundingRate", 0)) > 0 for f in funding_history[-3:]) else \
                   "負值" if all(float(f.get("fundingRate", 0)) < 0 for f in funding_history[-3:]) else "混合"

        close_prices = [float(k[4]) for k in klines[-48:]] if klines else [market_price]

        prompt = f"""你是加密貨幣合約網格交易監控 AI。請以繁體中文分析以下網格狀態並給出操作建議。

## 本輪網格設定
交易對: {symbol} | {grid_type_label} {grid_count} 格 | 槓桿 {leverage}x | 方向 {direction}
價格範圍: ${lower_price:,.2f} ~ ${upper_price:,.2f}
止損: {"$"+f"{stop_loss_price:,.2f}" if stop_loss_price else "未設"} | 止盈: {"$"+f"{take_profit_price:,.2f}" if take_profit_price else "未設"}
投入: ${invested_amount:,.2f} USDC | 開倉: {session_start.strftime("%m/%d %H:%M UTC")} | 已運行 {hours_running:.1f}h

## 當前市況
標記價: ${market_price:,.4f}
距上界: {dist_upper_pct:+.2f}% | 距下界: {dist_lower_pct:+.2f}%
本輪已實現損益: ${realized_pnl:+.4f}
當前 Funding Rate: {funding_rate*100:.4f}% | 近期趨勢: {fr_trend}

## K 線收盤價（最近 48 根 1h）
{close_prices}

請用 Python 計算以下指標（使用 numpy）：
- RSI(14)
- 布林通道(20, 2std): 上軌、中軌、下軌
- ATR(14)（假設 High≈Close*1.003, Low≈Close*0.997 估算）

## 重要事件搜尋
請判斷以下是否有影響價格的宏觀事件（用你的知識判斷，當前日期 {now.strftime("%Y-%m-%d")}）：
- 未來 48h 內是否有 FOMC/Fed 利率決議？
- 是否有重大加密貨幣監管新聞？
- ETH 鏈上是否有重大升級或事件？

## 輸出格式（嚴格按照此格式）
━━━━━ {symbol} 網格監控 {now.strftime("%H:%M")} ━━━━━
📊 技術指標: RSI [值] | BB [上軌]~[下軌] | ATR [值]
📈 距上界 [值]% | 距下界 [值]% | 已運行 {hours_running:.1f}h

🔧 操作建議: [A/B/C] — [一句話說明]
（A=持續觀察 B=注意風險 C=建議重開）

📅 重要事件: [若有填寫，否則省略此行]
📰 市況摘要: [2句話]

本輪損益: ${realized_pnl:+.4f} | 預估年化: [計算值或N/A]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

        try:
            response = await client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(code_execution=types.ToolCodeExecution())],
                    temperature=0.3,
                    max_output_tokens=1500,
                ),
            )
            # Extract text parts (skip code execution output blocks)
            text_parts = []
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    text_parts.append(part.text)
            return "\n".join(text_parts).strip() or response.text

        except Exception as exc:
            logger.error("gemini_monitor_failed", error=str(exc))
            return f"⚠️ 監控分析失敗：{str(exc)[:200]}"

    async def recommend_grid(
        self,
        symbol: str,
        market,
        klines: list,
        funding_history: list[dict],
        recent_sessions: list[dict],
        total_closed_profit: float,
    ) -> str:
        """Two-step grid parameter recommendation:
        Step 1 — Google Search Grounding for real-time market context.
        Step 2 — Structured recommendation with concrete Binance parameters.
        """
        client = self._ensure_client()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        # ── Step 1: Search Grounding for market context ──────────────
        search_prompt = (
            f"今天是 {now.strftime('%Y-%m-%d')}。"
            f"請搜尋以下資訊並用繁體中文整理成分析摘要（200字以內）：\n"
            f"1. {symbol[:3]}（以太坊）目前技術面走勢、支撐阻力位\n"
            f"2. 未來 48 小時內是否有 FOMC 或重大宏觀事件？\n"
            f"3. 加密貨幣市場近期重大新聞或情緒指標\n"
            f"4. ETH 資金費率在各大交易所的趨勢\n"
            f"輸出格式：技術面 / 宏觀事件 / 市場情緒 / 資金費率"
        )

        try:
            search_resp = await client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=search_prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                    max_output_tokens=600,
                ),
            )
            market_context = search_resp.text or "（搜尋失敗，僅用本地數據分析）"
        except Exception as exc:
            logger.warning("gemini_search_grounding_failed", error=str(exc))
            market_context = "（搜尋不可用，僅用本地數據分析）"

        # ── Step 2: Structured recommendation ────────────────────────
        close_prices = [float(k[4]) for k in klines[-48:]] if klines else [market.current_price]
        fr_values = [float(f.get("fundingRate", 0)) * 100 for f in funding_history]
        avg_fr = sum(fr_values) / len(fr_values) if fr_values else 0

        # Session history summary
        session_lines = []
        for s in recent_sessions[:6]:
            if not s.get("is_active"):
                dur_h = ((s.get("closed_at_ms", 0) - s["created_at_ms"]) / 3_600_000) if s.get("closed_at_ms") else 0
                gt = s.get("grid_type", "?")
                gc = s.get("grid_count", "?")
                lp = s.get("lower_price", "?")
                up = s.get("upper_price", "?")
                pnl = s.get("net_profit", 0) or 0
                session_lines.append(
                    f"  • {gt} {gc}格 ${lp}~${up} → {pnl:+.2f} USDC | {dur_h:.1f}h"
                )
        history_block = "\n".join(session_lines) if session_lines else "  （無歷史紀錄）"

        recommend_prompt = f"""你是加密貨幣合約網格交易顧問。請根據以下資訊，推薦在幣安開立 {symbol} 合約網格的最佳參數。

## 即時市場情報（Google Search）
{market_context}

## 幣安 API 市場數據
標記價: ${market.current_price:,.4f}
24h 高: ${market.high_24h:,.2f} | 24h 低: ${market.low_24h:,.2f} | 24h 漲跌: {market.price_change_pct_24h:+.2f}%
當前 Funding Rate: {market.funding_rate*100:.4f}% | 平均 FR（近10次）: {avg_fr:.4f}%
最近 48 根 1h 收盤價: {close_prices}

## 歷史輪次績效（最近 6 輪）
{history_block}
累計已關閉輪次獲利: ${total_closed_profit:+.4f} USDC

## 推薦格式（嚴格按照此格式，方便用戶填入幣安）
請用 Python + numpy 先計算 ATR(14) 和布林通道來輔助決定價格範圍，然後輸出：

━━━━━ Gemini 網格建議 {now.strftime("%m/%d %H:%M")} ━━━━━
交易對: {symbol}
方向: [NEUTRAL/LONG/SHORT]
類型: [等比(GEO)/等差(ARITHMETIC)]
格子數: [建議值]
下限價格: $[值]
上限價格: $[值]
止損價格: $[值]（ATR 依據）
止盈價格: $[值]
槓桿: [值]x

📊 技術分析依據:
  ATR(14): $[值] | BB 上: $[值] 下: $[值]
  [2-3 句說明為何選此範圍]

📅 注意事項:
  [若有重大事件或風險，否則省略]

💡 與歷史最佳輪次比較:
  [1 句話說明和過去哪輪相似或差異]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

        try:
            resp = await client.aio.models.generate_content(
                model=self._settings.gemini_model,
                contents=recommend_prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(code_execution=types.ToolCodeExecution())],
                    temperature=0.3,
                    max_output_tokens=2000,
                ),
            )
            text_parts = []
            for part in resp.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    text_parts.append(part.text)
            return "\n".join(text_parts).strip() or resp.text

        except Exception as exc:
            logger.error("gemini_recommend_failed", error=str(exc))
            return f"❌ 推薦生成失敗：{str(exc)[:200]}"

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
