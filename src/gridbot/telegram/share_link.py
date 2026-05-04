"""Binance grid bot share link parser.

Parses the base64-encoded `opt` parameter from Binance futures grid share links.
Example URL:
  https://app.binance.com/uni-qr/futuresgrid?...&opt=<base64>&coin=um

Decoded opt fields:
  s   = symbol (e.g. ETHUSDC)
  d   = direction (NEUTRAL / LONG / SHORT)
  gt  = grid type (GEO / ARITHMETIC)
  l   = leverage (int)
  gc  = grid count (int)
  lp  = lower price (float)
  up  = upper price (float)
  ssp = stop loss price (float)
  stp = take profit price (float)
  csi = strategy ID (str)
  im  = investment amount (float)
"""

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


SHARE_LINK_PATTERN = re.compile(
    r"https?://app\.binance\.com/uni-qr/futuresgrid[^\s]*", re.IGNORECASE
)


@dataclass
class ParsedGridConfig:
    symbol: str
    direction: str          # NEUTRAL / LONG / SHORT
    grid_type: str          # GEO / ARITHMETIC
    leverage: int
    grid_count: int
    lower_price: float
    upper_price: float
    stop_loss_price: float | None
    take_profit_price: float | None
    strategy_id: str | None
    investment_amount: float
    share_link: str


def extract_share_link(text: str) -> str | None:
    """Extract Binance grid share link from message text."""
    match = SHARE_LINK_PATTERN.search(text)
    return match.group(0) if match else None


def parse_share_link(url: str) -> ParsedGridConfig | None:
    """Parse a Binance futures grid share link into a structured config.

    Returns None if the URL is not a valid grid share link or cannot be decoded.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        opt_b64 = qs.get("opt", [None])[0]
        if not opt_b64:
            return None

        # Decode base64 → query string
        decoded = base64.b64decode(opt_b64 + "==").decode("utf-8")
        fields = dict(pair.split("=", 1) for pair in decoded.split("&") if "=" in pair)

        symbol = fields.get("s", "")
        if not symbol:
            return None

        direction_map = {"NEUTRAL": "NEUTRAL", "LONG": "LONG", "SHORT": "SHORT"}
        grid_type_map = {"GEO": "GEO", "ARITHMETIC": "ARITHMETIC"}

        return ParsedGridConfig(
            symbol=symbol,
            direction=direction_map.get(fields.get("d", ""), "NEUTRAL"),
            grid_type=grid_type_map.get(fields.get("gt", ""), "GEO"),
            leverage=int(fields.get("l", 1)),
            grid_count=int(fields.get("gc", 0)),
            lower_price=float(fields.get("lp", 0)),
            upper_price=float(fields.get("up", 0)),
            stop_loss_price=float(fields["ssp"]) if fields.get("ssp") else None,
            take_profit_price=float(fields["stp"]) if fields.get("stp") else None,
            strategy_id=fields.get("csi"),
            investment_amount=float(fields.get("im", 0)),
            share_link=url,
        )
    except Exception:
        return None


def format_config_confirmation(cfg: ParsedGridConfig, session_id: int | None, created_at_ms: int) -> str:
    """Format a Telegram confirmation message after successfully parsing a share link."""
    from datetime import datetime, timezone

    created_str = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc).strftime("%m/%d %H:%M UTC")
    grid_type_label = "等比" if cfg.grid_type == "GEO" else "等差"
    dir_emoji = {"NEUTRAL": "➡️", "LONG": "📈", "SHORT": "📉"}.get(cfg.direction, "➡️")

    lines = [
        f"✅ <b>網格設定已記錄</b>",
        f"",
        f"交易對: <b>{cfg.symbol}</b>",
        f"類型: <b>{grid_type_label} {cfg.grid_count} 格</b>",
        f"範圍: <b>${cfg.lower_price:,.2f} ~ ${cfg.upper_price:,.2f}</b>",
        f"槓桿: <b>{cfg.leverage}x</b>  {dir_emoji} 方向: <b>{cfg.direction}</b>",
    ]
    if cfg.stop_loss_price:
        lines.append(f"止損: <b>${cfg.stop_loss_price:,.2f}</b>  止盈: <b>${cfg.take_profit_price:,.2f}</b>" if cfg.take_profit_price else f"止損: <b>${cfg.stop_loss_price:,.2f}</b>")
    lines += [
        f"投入: <b>${cfg.investment_amount:,.2f} USDC</b>",
        f"開倉時間: <b>{created_str}</b>",
    ]
    if session_id:
        lines.append(f"Session #{session_id}")

    return "\n".join(lines)
