"""Telegram bot command handlers."""

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import FuturesTrade, IncomeRecord, MarketSnapshot, PositionInfo
from src.gridbot.grid.analyzer import compute_metrics
from src.gridbot.grid.models import GridMetrics
from src.gridbot.storage.repositories import (
    FuturesTradeRepository,
    GridSessionRepository,
    IncomeRepository,
)
from src.gridbot.telegram.formatters import (
    format_full_report,
    format_pnl_summary,
    format_risk_dashboard,
    format_sessions,
)
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Welcome message."""
    await update.message.reply_text(
        "🤖 <b>Binance 合約網格監控 Bot</b>\n\n"
        "我會定期監控你的網格機器人表現，\n"
        "並透過 Gemini AI 提供策略建議。\n\n"
        "輸入 /help 查看所有可用指令。",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — List all commands."""
    await update.message.reply_text(
        "📋 <b>可用指令</b>\n\n"
        "/status — 所有交易對即時狀態\n"
        "/sessions — 網格輪次歷史\n"
        "/pnl — 累計損益報表\n"
        "/risk — 風險儀表板\n"
        "/recommend — Gemini 推薦 ETHUSDC 網格參數\n"
        "/ask <code>問題</code> — 向 Gemini 提問\n"
        "/pause — 暫停定期監控\n"
        "/resume — 恢復定期監控\n"
        "/help — 列出所有指令\n\n"
        "💡 <i>直接傳送幣安網格分享連結，Bot 會自動解析並記錄設定。</i>",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — Current status for all or specific symbol."""
    app_data = context.application.bot_data

    binance_client: BinanceFuturesClient = app_data["binance_client"]
    settings: Settings = app_data["settings"]
    income_repo: IncomeRepository = app_data["income_repo"]
    session_repo: GridSessionRepository = app_data["session_repo"]
    trade_repo: FuturesTradeRepository = app_data["trade_repo"]

    args = context.args
    target_symbols = settings.symbols_list
    if args and args[0].upper() in target_symbols:
        target_symbols = [args[0].upper()]

    await update.message.reply_text("⏳ 正在取得最新資料...")

    try:
        all_metrics: dict[str, GridMetrics] = {}
        all_markets: dict[str, MarketSnapshot] = {}
        all_positions: dict[str, PositionInfo | None] = {}

        for symbol in target_symbols:
            result = await binance_client.fetch_symbol_data(symbol)
            income_records = [
                IncomeRecord.from_api({
                    "tranId": r["tran_id"], "symbol": r.get("symbol", ""),
                    "incomeType": r["income_type"], "income": str(r["income"]),
                    "asset": r["asset"], "time": r["time_ms"],
                    "info": r.get("info", ""), "tradeId": r.get("trade_id", ""),
                })
                for r in await income_repo.get_records(symbol=symbol, grid_only=True)
            ]

            grid_trade_rows = await trade_repo.get_trades(symbol, grid_only=True)
            grid_trades = [FuturesTrade.from_db(r) for r in grid_trade_rows]

            active_session = await session_repo.get_active_session(symbol=symbol)
            session_invested = active_session["invested_amount"] if active_session else None
            session_start = active_session["created_at_ms"] if active_session else None

            all_metrics[symbol] = compute_metrics(
                result,
                income_records=income_records,
                grid_trades=grid_trades,
                session_invested=session_invested,
                session_start_ms=session_start,
            )
            all_markets[symbol] = result.market
            all_positions[symbol] = result.position

        account = await binance_client.get_account_info()
        report = format_full_report(
            metrics=all_metrics,
            markets=all_markets,
            positions=all_positions,
            account_balance=account.total_margin_balance,
            margin_ratio=account.margin_ratio,
        )
        await update.message.reply_text(report, parse_mode="HTML")

    except Exception as exc:
        logger.error("cmd_status_failed", error=str(exc))
        await update.message.reply_text(f"❌ 取得狀態失敗：{str(exc)[:200]}")


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/risk — Risk dashboard."""
    app_data = context.application.bot_data
    binance_client: BinanceFuturesClient = app_data["binance_client"]
    settings: Settings = app_data["settings"]

    try:
        positions: dict[str, PositionInfo | None] = {}
        for symbol in settings.symbols_list:
            positions[symbol] = await binance_client.get_position(symbol)

        account = await binance_client.get_account_info()
        report = format_risk_dashboard(
            positions=positions,
            margin_ratio=account.margin_ratio,
            margin_warning=settings.margin_ratio_warning,
            margin_critical=settings.margin_ratio_critical,
        )
        await update.message.reply_text(report, parse_mode="HTML")

    except Exception as exc:
        logger.error("cmd_risk_failed", error=str(exc))
        await update.message.reply_text(f"❌ 取得風險資訊失敗：{str(exc)[:200]}")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <question> — Free-form question to Gemini."""
    if not context.args:
        await update.message.reply_text("用法: /ask <你的問題>")
        return

    question = " ".join(context.args)
    analyzer: GeminiAnalyzer = context.application.bot_data["gemini_analyzer"]

    await update.message.reply_text("🤔 思考中...")
    answer = await analyzer.ask(question)
    await update.message.reply_text(answer)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sessions — Grid session history."""
    session_repo: GridSessionRepository = context.application.bot_data["session_repo"]
    sessions = await session_repo.get_sessions(limit=10)
    total_profit = await session_repo.get_total_profit()

    report = format_sessions(sessions, total_profit)
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pnl — Cumulative P&L summary."""
    income_repo: IncomeRepository = context.application.bot_data["income_repo"]
    session_repo: GridSessionRepository = context.application.bot_data["session_repo"]

    income_summary = {}
    for itype in ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"]:
        income_summary[itype] = await income_repo.sum_income(itype, grid_only=True)

    session_profit = await session_repo.get_total_profit()
    report = format_pnl_summary(income_summary, session_profit)
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause — Pause scheduled fetching."""
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        scheduler.pause()
        await update.message.reply_text("⏸ 已暫停定期監控")
    else:
        await update.message.reply_text("排程器未初始化")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resume — Resume scheduled fetching."""
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        scheduler.resume()
        await update.message.reply_text("▶️ 已恢復定期監控")
    else:
        await update.message.reply_text("排程器未初始化")


async def handle_share_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect Binance grid share links and record the config to the active session."""
    from src.gridbot.telegram.share_link import extract_share_link, parse_share_link, format_config_confirmation

    text = update.message.text or ""
    url = extract_share_link(text)
    if not url:
        return

    cfg = parse_share_link(url)
    if not cfg:
        await update.message.reply_text("⚠️ 無法解析分享連結，請確認連結格式正確。")
        return

    session_repo: GridSessionRepository = context.application.bot_data["session_repo"]

    active = await session_repo.get_active_session(symbol=cfg.symbol)
    if not active:
        all_active = await session_repo.get_all_active_sessions()
        active = next(
            (s for s in all_active
             if abs(s["invested_amount"] - cfg.investment_amount) < 0.02),
            None,
        )

    if not active:
        await update.message.reply_text(
            f"⚠️ 找不到投入金額 ${cfg.investment_amount:.2f} 的 active session。\n"
            f"請確認網格已啟動且 bot 已同步資料（等下一次 fetch cycle）。"
        )
        return

    await session_repo.update_grid_config(
        create_tran_id=active["create_tran_id"],
        config={
            "symbol": cfg.symbol,
            "direction": cfg.direction,
            "grid_type": cfg.grid_type,
            "leverage": cfg.leverage,
            "grid_count": cfg.grid_count,
            "lower_price": cfg.lower_price,
            "upper_price": cfg.upper_price,
            "stop_loss_price": cfg.stop_loss_price,
            "take_profit_price": cfg.take_profit_price,
            "strategy_id": cfg.strategy_id,
            "share_link": cfg.share_link,
        },
    )

    logger.info(
        "grid_config_recorded",
        symbol=cfg.symbol, grid_count=cfg.grid_count,
        lower=cfg.lower_price, upper=cfg.upper_price,
        session_create_tran_id=active["create_tran_id"],
    )

    reply = format_config_confirmation(cfg, active.get("id"), active["created_at_ms"])
    await update.message.reply_text(reply, parse_mode="HTML")


async def cmd_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recommend — Ask Gemini for optimal grid parameters for ETHUSDC right now."""
    app_data = context.application.bot_data
    analyzer: GeminiAnalyzer = app_data["gemini_analyzer"]
    binance_client: BinanceFuturesClient = app_data["binance_client"]
    session_repo: GridSessionRepository = app_data["session_repo"]

    await update.message.reply_text("🔍 正在搜尋市況並生成網格建議，請稍候...")

    try:
        symbol = "ETHUSDC"

        market = await binance_client.fetch_market_snapshot(symbol)
        klines = await binance_client.get_klines(symbol, interval="1h", limit=48)
        funding_history = await binance_client.get_funding_rate_history(symbol, limit=10)

        recent_sessions = await session_repo.get_sessions(symbol=symbol, limit=8)
        total_profit = await session_repo.get_total_profit(symbol=symbol)

        report = await analyzer.recommend_grid(
            symbol=symbol,
            market=market,
            klines=klines,
            funding_history=funding_history,
            recent_sessions=recent_sessions,
            total_closed_profit=total_profit,
        )

        await update.message.reply_text(report, parse_mode="HTML")

    except Exception as exc:
        logger.error("cmd_recommend_failed", error=str(exc))
        await update.message.reply_text(f"❌ 生成建議失敗：{str(exc)[:200]}")
