"""Telegram message formatters for grid bot reports."""

from datetime import datetime, timezone

from src.gridbot.ai.models import GeminiRecommendation
from src.gridbot.binance.models import MarketSnapshot, PositionInfo
from src.gridbot.grid.models import GridMetrics


def _grid_config_lines(session: dict) -> list[str]:
    """Build grid config block from a session dict (populated via share link)."""
    if not session.get("lower_price") or not session.get("upper_price"):
        return []

    gt = "等比(GEO)" if session.get("grid_type") == "GEO" else "等差(ARITHMETIC)"
    gc = session.get("grid_count") or "?"
    lp = session["lower_price"]
    up = session["upper_price"]
    lev = session.get("leverage") or "?"
    direction = session.get("direction") or "?"
    dir_emoji = {"NEUTRAL": "➡️", "LONG": "📈", "SHORT": "📉"}.get(str(direction), "➡️")

    lines = [
        f"🔧 <b>網格設定</b>",
        f"  {gt} | {gc} 格 | {lev}x 槓桿 | {dir_emoji} {direction}",
        f"  範圍: ${lp:,.2f} ~ ${up:,.2f}",
    ]
    if session.get("stop_loss_price"):
        sl = session["stop_loss_price"]
        tp = session.get("take_profit_price")
        if tp:
            lines.append(f"  止損: ${sl:,.2f} | 止盈: ${tp:,.2f}")
        else:
            lines.append(f"  止損: ${sl:,.2f}")
    elif session.get("take_profit_price"):
        lines.append(f"  止盈: ${session['take_profit_price']:,.2f}")

    invested = session.get("invested_amount")
    if invested:
        lines.append(f"  投入: ${invested:,.2f} USDC")

    created_ms = session.get("created_at_ms")
    if created_ms:
        created_str = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%m/%d %H:%M UTC")
        lines.append(f"  開倉: {created_str}")

    return lines


def format_symbol_report(
    symbol: str,
    metrics: GridMetrics,
    market: MarketSnapshot,
    position: PositionInfo | None = None,
    session: dict | None = None,
) -> str:
    """Format a single symbol's status report."""
    lines = [f"━━ {symbol} 永續合約 ━━"]

    # Grid config block (from share link) — most useful info first
    if session:
        cfg_lines = _grid_config_lines(session)
        if cfg_lines:
            lines.extend(cfg_lines)
            lines.append("")

    # Price & market
    lines.append(f"💰 標記價: ${market.current_price:,.2f}")
    lines.append(f"📊 24h: {market.price_change_pct_24h:+.2f}% | H: ${market.high_24h:,.2f} L: ${market.low_24h:,.2f}")

    if market.funding_rate is not None:
        fr_emoji = "📈" if market.funding_rate > 0 else "📉" if market.funding_rate < 0 else "➡️"
        lines.append(f"{fr_emoji} Funding Rate: {market.funding_rate*100:.4f}%")

    # Position
    if position and position.position_amt != 0:
        emoji = "📈" if position.position_direction == "LONG" else "📉"
        lines.append(f"{emoji} 持倉: {position.position_direction} {abs(position.position_amt):.4f} @ ${position.entry_price:,.2f}")
        lines.append(f"💹 未實現損益: ${position.unrealized_pnl:.4f}")
        if position.liquidation_price:
            lines.append(f"⚠️ 清算價: ${position.liquidation_price:,.2f}")
            if position.distance_to_liquidation_pct is not None:
                dist = position.distance_to_liquidation_pct
                risk_emoji = "🔴" if dist < 10 else "🟡" if dist < 15 else "🟢"
                lines.append(f"{risk_emoji} 距清算: {dist:.1f}%")

    # P&L
    pnl_emoji = "🟢" if metrics.net_pnl >= 0 else "🔴"
    lines.append(f"{pnl_emoji} 已實現損益: ${metrics.realized_pnl:.4f}")
    lines.append(f"💸 手續費: ${metrics.commission_total:.4f} | Funding: ${metrics.funding_cost:.4f}")
    lines.append(f"📊 淨損益: <b>${metrics.net_pnl:.4f}</b>")

    lines.append(f"🔄 成交: {metrics.total_trades} 筆 (Maker {metrics.maker_ratio:.0%})")

    if metrics.apr_estimate is not None:
        lines.append(f"📈 年化預估: {metrics.apr_estimate:.1f}%")

    if metrics.running_hours:
        lines.append(f"⏱ 已運行: {metrics.running_hours:.1f}h")

    return "\n".join(lines)


def format_full_report(
    metrics: dict[str, GridMetrics],
    markets: dict[str, MarketSnapshot],
    positions: dict[str, PositionInfo | None],
    account_balance: float | None = None,
    margin_ratio: float | None = None,
    sessions: dict[str, dict | None] | None = None,
) -> str:
    """Format a complete report for all symbols."""
    now = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M UTC")
    lines = [f"📊 <b>合約網格報告</b> — {now}", ""]

    if account_balance is not None:
        lines.append(f"🏦 帳戶餘額: ${account_balance:.2f}")
    if margin_ratio is not None:
        ratio_pct = margin_ratio * 100
        risk_emoji = "🔴" if ratio_pct > 80 else "🟡" if ratio_pct > 60 else "🟢"
        lines.append(f"{risk_emoji} 保證金率: {ratio_pct:.1f}%")
    lines.append("")

    for symbol in metrics:
        lines.append(format_symbol_report(
            symbol=symbol,
            metrics=metrics[symbol],
            market=markets[symbol],
            position=positions.get(symbol),
            session=sessions.get(symbol) if sessions else None,
        ))
        lines.append("")

    total_pnl = sum(m.net_pnl for m in metrics.values())
    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"━━ 總計 ━━")
    lines.append(f"{total_emoji} 總淨損益: ${total_pnl:.4f}")

    return "\n".join(lines)


def format_recommendation(rec: GeminiRecommendation, current_strategy: str) -> str:
    """Format a Gemini recommendation for Telegram (used by scheduled general analysis)."""
    def _fmt_num(value: float | int | None, digits: int = 2) -> str:
        if value is None:
            return "未提供"
        return str(value) if isinstance(value, int) else f"{value:.{digits}f}"

    lines = ["🤖 <b>Gemini AI 分析建議</b>", ""]

    strategy_emoji = "🔄" if rec.recommended_strategy != current_strategy else "✅"
    lines.append(f"{strategy_emoji} 策略: {current_strategy} → <b>{rec.recommended_strategy}</b>")
    lines.append(f"📊 信心度: {rec.confidence:.0%}")
    lines.append(f"⚡ 槓桿: {rec.leverage_suggestion}x")

    dir_emoji = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "➡️"}
    lines.append(f"{dir_emoji.get(rec.direction_suggestion, '➡️')} 方向: {rec.direction_suggestion}")

    if rec.parameter_adjustments:
        lines.append("")
        lines.append("📋 <b>參數調整:</b>")
        for adj in rec.parameter_adjustments:
            current = f"{adj.current_value}" if adj.current_value is not None else "?"
            lines.append(f"  • {adj.parameter}: {current} → <b>{adj.suggested_value}</b>")
            lines.append(f"    {adj.reason}")

    fp = rec.final_parameters
    lines.append("")
    lines.append("🧩 <b>最終建議參數（填入幣安）</b>")
    lines.append(f"  • 幣對: <b>{fp.symbol or '未提供'}</b>")
    lines.append(f"  • 價格範圍: <b>{_fmt_num(fp.lower_price)} - {_fmt_num(fp.upper_price)}</b>")
    lines.append(f"  • 網格數: <b>{_fmt_num(fp.grid_count, 0)}</b>")
    grid_type = {"ARITHMETIC": "等差", "GEOMETRIC": "等比"}.get(fp.grid_type or "", "未提供")
    lines.append(f"  • 網格類型: <b>{grid_type}</b>")
    lines.append(f"  • 投資額(USDC): <b>{_fmt_num(fp.investment_usdc)}</b>")
    lines.append(f"  • 槓桿: <b>{fp.leverage or rec.leverage_suggestion}x</b>")
    lines.append(f"  • 方向: <b>{fp.direction or rec.direction_suggestion}</b>")
    margin_mode = {"CROSS": "全倉", "ISOLATED": "逐倉"}.get(fp.margin_mode or "", "未提供")
    lines.append(f"  • 保證金模式: <b>{margin_mode}</b>")
    lines.append(f"  • 止盈價: <b>{_fmt_num(fp.take_profit_price)}</b>")
    lines.append(f"  • 止損價: <b>{_fmt_num(fp.stop_loss_price)}</b>")

    lines.append("")
    lines.append(f"📌 <b>市況:</b> {rec.market_condition_summary}")
    lines.append("")
    lines.append(f"💸 <b>Funding:</b> {rec.funding_rate_analysis}")
    lines.append("")
    lines.append(f"⚠️ <b>清算風險:</b> {rec.liquidation_risk_assessment}")

    if rec.risk_warnings:
        lines.append("")
        lines.append("🚨 <b>風險警告:</b>")
        for w in rec.risk_warnings:
            lines.append(f"  ⚠️ {w}")

    lines.append("")
    lines.append(f"💡 <b>推理:</b> {rec.reasoning}")
    lines.append("")
    lines.append("<i>[此為建議，請自行在幣安平台調整]</i>")

    return "\n".join(lines)


def format_sessions(sessions: list[dict], total_profit: float) -> str:
    """Format grid session history for /sessions command, including grid config."""
    lines = ["📋 <b>網格輪次歷史</b>", ""]

    for s in sessions[:10]:
        status = "🟢 運行中" if s.get("is_active") else "✅ 已關閉"
        symbol = s.get("symbol") or "未知"
        invested = s["invested_amount"]
        profit = s.get("net_profit")
        profit_emoji = "📈" if profit and profit > 0 else "📉" if profit and profit < 0 else "➡️"
        profit_str = f"${profit:+.4f}" if profit is not None else "計算中"

        created = datetime.fromtimestamp(
            s["created_at_ms"] / 1000, tz=timezone.utc
        ).strftime("%m/%d %H:%M")

        # Duration
        duration_str = ""
        if s.get("closed_at_ms"):
            dur_h = (s["closed_at_ms"] - s["created_at_ms"]) / 3_600_000
            duration_str = f" | {dur_h:.1f}h"
        elif s.get("is_active"):
            from datetime import datetime as _dt
            dur_h = (_dt.now(timezone.utc).timestamp() * 1000 - s["created_at_ms"]) / 3_600_000
            duration_str = f" | 運行 {dur_h:.1f}h"

        lines.append(f"{status} <b>{symbol}</b> — {created}{duration_str}")
        lines.append(f"  投入: ${invested:.2f} | {profit_emoji} {profit_str}")

        # Grid config (populated from share link)
        if s.get("lower_price") and s.get("upper_price"):
            gt = "等比" if s.get("grid_type") == "GEO" else "等差"
            gc = s.get("grid_count") or "?"
            lp = s["lower_price"]
            up = s["upper_price"]
            lev = s.get("leverage") or "?"
            direction = s.get("direction") or "?"
            dir_emoji = {"NEUTRAL": "➡️", "LONG": "📈", "SHORT": "📉"}.get(str(direction), "➡️")
            lines.append(f"  {gt} {gc}格 | {lev}x {dir_emoji}{direction} | ${lp:,.2f}~${up:,.2f}")

            sl = s.get("stop_loss_price")
            tp = s.get("take_profit_price")
            if sl or tp:
                sl_str = f"止損 ${sl:,.2f}" if sl else ""
                tp_str = f"止盈 ${tp:,.2f}" if tp else ""
                lines.append(f"  {' | '.join(x for x in [sl_str, tp_str] if x)}")
        else:
            lines.append(f"  （未記錄網格設定）")

        lines.append("")

    lines.append(f"━━ 累計已關閉輪次利潤: <b>${total_profit:.4f}</b> ━━")
    return "\n".join(lines)


def format_pnl_summary(income_summary: dict[str, float], session_profit: float) -> str:
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
