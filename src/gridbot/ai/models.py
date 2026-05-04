"""Gemini AI response models — Pydantic schemas for structured output."""

from pydantic import BaseModel, Field
from typing import Literal


class ParameterAdjustment(BaseModel):
    """A single parameter adjustment suggestion."""

    parameter: str = Field(description="參數名稱: grid_spacing_pct, num_grids, price_range_width_pct, leverage, direction")
    current_value: float | int | str | None = Field(default=None, description="當前值")
    suggested_value: float | int | str = Field(description="建議值")
    reason: str = Field(description="調整原因（繁體中文）")


class FinalGridParameters(BaseModel):
    """Final Binance futures grid parameters to display to user."""

    symbol: str | None = Field(default=None, description="交易對，例如 ETHUSDC")
    lower_price: float | None = Field(default=None, description="最低價格")
    upper_price: float | None = Field(default=None, description="最高價格")
    grid_count: int | None = Field(default=None, description="網格數")
    grid_type: Literal["ARITHMETIC", "GEOMETRIC"] | None = Field(
        default=None, description="網格類型：等差或等比"
    )
    investment_usdc: float | None = Field(default=None, description="保證金投入（USDC）")
    leverage: int | None = Field(default=None, ge=1, le=10, description="槓桿倍數")
    direction: Literal["LONG", "SHORT", "NEUTRAL"] | None = Field(
        default=None, description="方向偏好"
    )
    margin_mode: Literal["CROSS", "ISOLATED"] | None = Field(
        default=None, description="保證金模式"
    )
    stop_loss_price: float | None = Field(default=None, description="止損價格")
    take_profit_price: float | None = Field(default=None, description="止盈價格")
    close_all_on_stop: bool | None = Field(
        default=True, description="終止時是否全部平倉"
    )


class GeminiRecommendation(BaseModel):
    """Structured response from Gemini AI analysis.

    This schema is injected into Gemini via response_schema to enforce
    structured JSON output.
    """

    recommended_strategy: Literal[
        "conservative", "moderate", "aggressive", "range_bound", "trending"
    ] = Field(description="建議的策略名稱")

    confidence: float = Field(
        ge=0.0, le=1.0,
        description="信心分數 (0.0=極不確定, 1.0=極度確定)"
    )

    parameter_adjustments: list[ParameterAdjustment] = Field(
        default_factory=list,
        description="具體的參數調整建議列表"
    )
    final_parameters: FinalGridParameters = Field(
        default_factory=FinalGridParameters,
        description="最終可直接填入幣安合約網格的建議參數"
    )

    leverage_suggestion: int = Field(
        ge=1, le=10,
        description="建議的槓桿倍數"
    )

    direction_suggestion: Literal["LONG", "SHORT", "NEUTRAL"] = Field(
        description="建議的方向偏差"
    )

    market_condition_summary: str = Field(
        description="當前市況摘要（繁體中文，2-3 句）"
    )

    reasoning: str = Field(
        description="策略建議的詳細推理過程（繁體中文）"
    )

    risk_warnings: list[str] = Field(
        default_factory=list,
        description="風險警告列表（繁體中文）"
    )

    funding_rate_analysis: str = Field(
        description="Funding Rate 趨勢分析（繁體中文）"
    )

    liquidation_risk_assessment: str = Field(
        description="清算風險評估（繁體中文）"
    )
