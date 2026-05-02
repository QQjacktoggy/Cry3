"""Telegram message formatters for grid bot reports.

Formats metrics, positions, and recommendations into rich Telegram messages.
Uses HTML parse mode for styling.
"""

from datetime import datetime, timezone

from src.gridbot.ai.models import GeminiRecommendation
from src.gridbot.binance.models import MarketSnapshot, PositionInfo
from src.gridbot.grid.models import GridMetrics


def format_symbol_report(
    symbol: str,
    metrics: GridMetrics,
    market: MarketSnapshot,
    position: PositionInfo | None = None,
) -> str:
    """Format a single symbol's status report."""
    lines = [f"━━ {symbol} 永續合約 ━━"]

    # Price
    lines.append(f"💰 標記價: ${market.current_price:,.2f}")
    lines.append(f"📊 24h: {market.price_change_pct_24h:+.2f}% | H: ${market.high_24h:,.2f} L: ${market.low_24h:,.2f}")

    # Position
    if position and position.position_amt != 0:
        emoji = "📈" if position.position_direction == "LONG" else "📉"
        lines.append(f"{emoji} 持倉: {position.position_direction} {abs(position.position_amt)} @ ${position.entry_price:,.2f}")
        lines.append(f"⚡ 槓桿: {position.leverage}x | 逐倉")
        lines.append(f"💹 未實現損益: ${position.unrealized_pnl:.4f}")
        lines.append(f"⚠️ 清算價: ${position.liquidation_price:,.2f}", )
        if position.distance_to_liquidation_pct is not None:
            dist = position.distance_to_liquidation_pct
            risk_emoji = "🔴" if dist < 10 else "🟡" if dist < 15 else "🟢"
            lines.append(f"{risk_emoji} 距清算: {dist:.1f}%")
    else:
        lines.append("📋 持倉: 無")

    # P&L
    pnl_emoji = "🟢" if metrics.net_pnl >= 0 else "🔴"
    lines.append(f"{pnl_emoji} 已實現損益: ${metrics.realized_pnl:.4f}")
    lines.append(f"💸 手續費: ${metrics.commission_total:.4f} | Funding: ${metrics.funding_cost:.4f}")
    lines.append(f"📊 淨損益: ${metrics.net_pnl:.4f}")

    # Trade stats
    lines.append(f"🔄 交易: {metrics.total_trades} 筆 (Maker: {metrics.maker_ratio:.0%})")

    # Funding rate
    if market.funding_rate is not None:
        fr_emoji = "📈" if market.funding_rate > 0 else "📉" if market.funding_rate < 0 else "➡️"
        lines.append(f"{fr_emoji} Funding Rate: {market.funding_rate*100:.4f}%")

    # APR
    if metrics.apr_estimate is not None:
        lines.append(f"📈 年化預估: {metrics.apr_estimate:.1f}%")

    # Investment
    if metrics.investment_amount is not None:
        lines.append(f"📐 本輪投入: ${metrics.investment_amount:.2f}")

    return "\n".join(lines)


def format_full_report(
    metrics: dict[str, GridMetrics],
    markets: dict[str, MarketSnapshot],
    positions: dict[str, PositionInfo | None],
    account_balance: float | None = None,
    margin_ratio: float | None = None,
) -> str:
    """Format a complete report for all symbols."""
    now = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M UTC")
    lines = [f"📊 <b>合約網格報告</b> — {now}", ""]

    # Account summary
    if account_balance is not None:
        lines.append(f"🏦 帳戶餘額: ${account_balance:.2f}")
    if margin_ratio is not None:
        ratio_pct = margin_ratio * 100
        risk_emoji = "🔴" if ratio_pct > 80 else "🟡" if ratio_pct > 60 else "🟢"
        lines.append(f"{risk_emoji} 保證金率: {ratio_pct:.1f}%")
    lines.append("")

    # Per-symbol reports
    for symbol in metrics:
        lines.append(format_symbol_report(
            symbol=symbol,
            metrics=metrics[symbol],
            market=markets[symbol],
            position=positions.get(symbol),
        ))
        lines.append("")

    # Total P&L across all symbols
    total_pnl = sum(m.net_pnl for m in metrics.values())
    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"━━ 總計 ━━")
    lines.append(f"{total_emoji} 總淨損益: ${total_pnl:.4f}")

    return "\n".join(lines)


def format_recommendation(rec: GeminiRecommendation, current_strategy: str) -> str:
    """Format a Gemini recommendation for Telegram."""
    lines = ["🤖 <b>Gemini AI 分析建議</b>", ""]

    # Strategy change
    strategy_emoji = "🔄" if rec.recommended_strategy != current_strategy else "✅"
    lines.append(f"{strategy_emoji} 策略: {current_strategy} → <b>{rec.recommended_strategy}</b>")
    lines.append(f"📊 信心度: {rec.confidence:.0%}")
    lines.append(f"⚡ 槓桿: {rec.leverage_suggestion}x")

    dir_emoji = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "➡️"}
    lines.append(f"{dir_emoji.get(rec.direction_suggestion, '➡️')} 方向: {rec.direction_suggestion}")

    # Parameter adjustments
    if rec.parameter_adjustments:
        lines.append("")
        lines.append("📋 <b>參數調整:</b>")
        for adj in rec.parameter_adjustments:
            current = f"{adj.current_value}" if adj.current_value is not None else "?"
            lines.append(f"  • {adj.parameter}: {current} → <b>{adj.suggested_value}</b>")
            lines.append(f"    {adj.reason}")

    # Market summary
    lines.append("")
    lines.append(f"📌 <b>市況:</b> {rec.market_condition_summary}")

    # Funding rate analysis
    lines.append("")
    lines.append(f"💸 <b>Funding:</b> {rec.funding_rate_analysis}")

    # Liquidation risk
    lines.append("")
    lines.append(f"⚠️ <b>清算風險:</b> {rec.liquidation_risk_assessment}")

    # Risk warnings
    if rec.risk_warnings:
        lines.append("")
        lines.append("🚨 <b>風險警告:</b>")
        for w in rec.risk_warnings:
            lines.append(f"  ⚠️ {w}")

    # Reasoning
    lines.append("")
    lines.append(f"💡 <b>推理:</b> {rec.reasoning}")

    lines.append("")
    lines.append("<i>[此為建議，請自行在幣安平台調整]</i>")

    return "\n".join(lines)


def format_risk_dashboard(
    positions: dict[str, PositionInfo | None],
    margin_ratio: float | None = None,
    margin_warning: float = 0.6,
    margin_critical: float = 0.8,
) -> str:
    """Format risk dashboard for /risk command."""
    lines = ["🛡️ <b>風險儀表板</b>", ""]

    # Overall margin
    if margin_ratio is not None:
        ratio_pct = margin_ratio * 100
        if ratio_pct > margin_critical * 100:
            status = "🔴 危險"
        elif ratio_pct > margin_warning * 100:
            status = "🟡 警戒"
        else:
            status = "🟢 安全"
        lines.append(f"保證金率: {ratio_pct:.1f}% {status}")
    else:
        lines.append("保證金率: N/A")
    lines.append("")

    # Per-symbol risk
    for symbol, pos in positions.items():
        lines.append(f"━━ {symbol} ━━")
        if pos and pos.position_amt != 0:
            lines.append(f"  方向: {pos.position_direction} | 槓桿: {pos.leverage}x")
            lines.append(f"  清算價: ${pos.liquidation_price:,.2f}")
            if pos.distance_to_liquidation_pct is not None:
                dist = pos.distance_to_liquidation_pct
                if dist < 10:
                    risk = "🔴 高危"
                elif dist < 15:
                    risk = "🟡 警戒"
                elif dist < 20:
                    risk = "🟢 安全"
                else:
                    risk = "🟢 極安全"
                lines.append(f"  距清算: {dist:.1f}% {risk}")
            lines.append(f"  未實現損益: ${pos.unrealized_pnl:.4f}")
        else:
            lines.append("  無持倉")
        lines.append("")

    return "\n".join(lines)


def format_sessions(sessions: list[dict], total_profit: float) -> str:
    """Format grid session history for /sessions command."""
    lines = ["📋 <b>網格輪次歷史</b>", ""]

    for s in sessions[:10]:
        status = "🟢 運行中" if s.get("is_active") else "✅ 已關閉"
        symbol = s.get("symbol") or "未知"
        invested = s["invested_amount"]
        profit = s.get("net_profit")
        profit_str = f"${profit:.4f}" if profit is not None else "計算中"
        profit_emoji = "📈" if profit and profit > 0 else "📉" if profit and profit < 0 else "➡️"

        created = datetime.fromtimestamp(
            s["created_at_ms"] / 1000, tz=timezone.utc
        ).strftime("%m/%d %H:%M")

        lines.append(f"{status} {symbol} | {created}")
        lines.append(f"  投入: ${invested:.2f} | {profit_emoji} 損益: {profit_str}")
        lines.append("")

    lines.append(f"━━ 累計已關閉輪次利潤: <b>${total_profit:.4f}</b> ━━")

    return "\n".join(lines)


def format_pnl_summary(
    income_summary: dict[str, float],
    session_profit: float,
) -> str:
    """Format P&L summary for /pnl command."""
    lines = ["💰 <b>累計損益報表</b>", ""]

    realized = income_summary.get("REALIZED_PNL", 0)
    commission = abs(income_summary.get("COMMISSION", 0))
    funding = abs(income_summary.get("FUNDING_FEE", 0))
    net = realized - commission - funding

    lines.append(f"📈 已實現損益: ${realized:.4f}")
    lines.append(f"💸 手續費: -${commission:.4f}")
    lines.append(f"📊 Funding 費用: -${funding:.4f}")
    lines.append(f"{'🟢' if net >= 0 else '🔴'} 淨損益: <b>${net:.4f}</b>")
    lines.append("")
    lines.append(f"🔄 網格輪次利潤: <b>${session_profit:.4f}</b>")

    return "\n".join(lines)
