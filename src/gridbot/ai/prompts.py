"""Prompt construction for Gemini AI analysis.

Builds system prompts and user prompts from live data,
injecting strategy bounds and current metrics.
"""

import json
from datetime import datetime, timezone

from config.strategies import get_strategies_for_prompt
from src.gridbot.grid.models import GridMetrics
from src.gridbot.binance.models import MarketSnapshot, PositionInfo


SYSTEM_PROMPT = """你是一個 Binance USD-M 永續合約網格交易策略顧問。你的角色嚴格限制在以下範圍：

## 你的職責
1. 分析提供的市場數據和網格機器人表現指標
2. 從預定義策略中選擇最適合當前市況的策略
3. 在所選策略的參數邊界內建議調整，包含：
   - 格距百分比 (grid_spacing_pct)
   - 格子數 (num_grids)
   - 價格區間寬度 (price_range_width_pct)
   - 槓桿倍數 (leverage)
   - 方向偏差 (direction: LONG/SHORT/NEUTRAL)
4. 分析 Funding Rate 趨勢及其對持倉成本的影響
5. 評估清算風險，在距離清算價過近時發出警告

## 嚴格約束
- 你只能從提供的策略列表中選擇，不能創建新策略
- 所有參數建議必須在策略的邊界範圍內
- 你不能建議具體的買賣操作或市價單
- 你不能預測未來價格走向（可以分析趨勢但不做價格預測）
- 信心分數必須真實反映不確定性（不要給過高的信心值）
- 風險警告必須具體且與當前條件相關

## 合約交易分析維度
- **Funding Rate**：持續正值表示多頭擁擠（做多成本高），持續負值表示空頭擁擠
- **保證金率**：接近維持保證金時需降低槓桿或縮小區間
- **清算距離**：距離清算價 <10% 為高危，<15% 為警戒，>20% 為安全
- **持倉方向**：根據趨勢和 funding rate 建議偏多/偏空/中性
- **手續費結構**：Maker 0%、Taker 0.04%，應優先建議增加 Maker 成交比例的策略

## 可用策略
{strategies_json}

## 輸出要求
- 以繁體中文回答
- 分析要具體、有數據支撐
- 風險警告要列出真實風險，不要泛泛而談
"""


def build_system_prompt() -> str:
    """Build the system prompt with strategy definitions injected."""
    strategies = get_strategies_for_prompt()
    return SYSTEM_PROMPT.format(
        strategies_json=json.dumps(strategies, indent=2, ensure_ascii=False)
    )


def build_user_prompt(
    metrics: dict[str, GridMetrics],
    markets: dict[str, MarketSnapshot],
    positions: dict[str, PositionInfo | None],
    funding_rates: dict[str, list[dict]],
    current_strategy: str,
    account_balance: float | None = None,
    margin_ratio: float | None = None,
) -> str:
    """Build the user prompt from current data.

    Args:
        metrics: Symbol -> GridMetrics mapping
        markets: Symbol -> MarketSnapshot mapping
        positions: Symbol -> PositionInfo mapping
        funding_rates: Symbol -> list of recent funding rate records
        current_strategy: Current strategy name
        account_balance: Total account margin balance
        margin_ratio: Current maintenance margin / margin balance
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = [f"## 分析時間: {now}", f"## 當前策略: {current_strategy}", ""]

    # Account overview
    sections.append("## 帳戶概覽")
    if account_balance is not None:
        sections.append(f"- 保證金餘額: ${account_balance:.2f}")
    if margin_ratio is not None:
        sections.append(f"- 保證金使用率: {margin_ratio:.1%}")
    sections.append("")

    # Per-symbol data
    for symbol in metrics:
        m = metrics[symbol]
        mkt = markets.get(symbol)
        pos = positions.get(symbol)
        fr = funding_rates.get(symbol, [])

        sections.append(f"## {symbol}")
        sections.append("")

        # Market data
        if mkt:
            sections.append("### 市場數據")
            sections.append(f"- 當前價格: ${mkt.current_price:,.2f}")
            sections.append(f"- Mark Price: ${mkt.mark_price:,.2f}" if mkt.mark_price else "")
            sections.append(f"- 24h 高/低: ${mkt.high_24h:,.2f} / ${mkt.low_24h:,.2f}")
            sections.append(f"- 24h 漲跌: {mkt.price_change_pct_24h:+.2f}%")
            sections.append(f"- 24h 成交量: ${mkt.volume_24h:,.0f}")
            if mkt.funding_rate is not None:
                sections.append(f"- 當前 Funding Rate: {mkt.funding_rate:.6f} ({mkt.funding_rate*100:.4f}%)")
            sections.append("")

        # Funding rate history
        if fr:
            sections.append("### Funding Rate 歷史 (最近)")
            for entry in fr[-5:]:
                rate = float(entry.get("fundingRate", 0))
                ts = int(entry.get("fundingTime", 0))
                time_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m/%d %H:%M")
                sections.append(f"- {time_str}: {rate:.6f} ({rate*100:.4f}%)")
            sections.append("")

        # Position info
        if pos and pos.position_amt != 0:
            sections.append("### 持倉資訊")
            sections.append(f"- 方向: {pos.position_direction}")
            sections.append(f"- 數量: {abs(pos.position_amt)}")
            sections.append(f"- 入場均價: ${pos.entry_price:,.2f}")
            sections.append(f"- 槓桿: {pos.leverage}x")
            sections.append(f"- 未實現損益: ${pos.unrealized_pnl:.4f}")
            sections.append(f"- 清算價: ${pos.liquidation_price:,.2f}")
            if pos.distance_to_liquidation_pct is not None:
                sections.append(f"- 距清算價: {pos.distance_to_liquidation_pct:.1f}%")
            sections.append(f"- 逐倉保證金: ${pos.isolated_margin:.2f}" if pos.isolated_margin else "")
            sections.append("")
        else:
            sections.append("### 持倉資訊")
            sections.append("- 無持倉（網格可能剛關閉或正在建立）")
            sections.append("")

        # Grid metrics
        sections.append("### 網格表現")
        sections.append(f"- 已實現損益: ${m.realized_pnl:.4f}")
        sections.append(f"- 手續費: ${m.commission_total:.4f}")
        sections.append(f"- Funding 費用: ${m.funding_cost:.4f}")
        sections.append(f"- 淨損益: ${m.net_pnl:.4f}")
        sections.append(f"- 總交易筆數: {m.total_trades}")
        sections.append(f"- Maker/Taker: {m.maker_trades}/{m.taker_trades} ({m.maker_ratio:.0%} Maker)")
        sections.append(f"- 交易頻率: {m.trades_per_hour:.1f} 筆/時")
        if m.apr_estimate is not None:
            sections.append(f"- 年化報酬率估算: {m.apr_estimate:.1f}%")
        if m.investment_amount is not None:
            sections.append(f"- 本輪投入資金: ${m.investment_amount:.2f}")
        sections.append(f"- 網格價格範圍: ${m.grid_lower_price:,.2f} ~ ${m.grid_upper_price:,.2f}" if m.grid_lower_price and m.grid_upper_price else "")
        sections.append(f"- 價格範圍利用率: {m.price_range_utilization:.0%}")
        sections.append("")

    # Final question
    sections.append("---")
    sections.append("根據以上數據，請分析當前市況並提供策略建議。")
    sections.append("特別注意：清算風險、Funding Rate 趨勢、以及 Maker/Taker 比例。")

    return "\n".join(s for s in sections if s is not None)
