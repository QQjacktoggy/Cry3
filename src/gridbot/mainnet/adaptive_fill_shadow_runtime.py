"""Read-only runtime for v1.4.58 STUP fill counterfactuals."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Mapping

from src.gridbot.mainnet.adaptive_fill_shadow import (
    build_stup_fill_shadow_samples,
    first_stup_shadow_fill,
    no_fill_outcome,
)
from src.gridbot.utils.logging import get_logger


logger = get_logger(__name__)


class AdaptiveStupFillShadowTracker:
    def __init__(
        self,
        *,
        client: Any,
        repo: Any,
        settings: Any,
        version: str,
        variants: Mapping[str, float],
        count_event: Callable[[str, str], None],
        samples: dict[str, dict[str, Any]],
        started_groups: set[str],
        unavailable_groups: set[str],
        lock: asyncio.Lock,
    ) -> None:
        self._client = client
        self._repo = repo
        self._settings = settings
        self._version = version
        self._variants = dict(variants)
        self._count_event = count_event
        self._samples = samples
        self._started_groups = started_groups
        self._unavailable_groups = unavailable_groups
        self._lock = lock

    async def start(
        self,
        *,
        run_id: str,
        session_id: str,
        opportunity_id: str,
        symbol: str,
        side: str,
        signal_price: float,
        notional_usdc: float,
        tp_pct: float,
        sl_pct: float,
        partial_exit_pct: float,
        action_id: str,
    ) -> bool:
        group_id = f"{session_id}:{opportunity_id}"
        if not session_id or not opportunity_id or group_id in self._started_groups:
            return False
        try:
            tick_size = float(await self._client.price_tick_size(symbol))
        except Exception as exc:  # noqa: BLE001 - telemetry cannot block execution
            logger.warning(
                "adaptive_stup_fill_shadow_tick_failed",
                run_id=run_id,
                error=str(exc)[:200],
            )
            return False
        samples = build_stup_fill_shadow_samples(
            group_id=group_id,
            run_id=run_id,
            session_id=session_id,
            symbol=symbol,
            side=side,
            signal_price=signal_price,
            tick_size=tick_size,
            start_ms=int(time.time() * 1000),
            decision_latency_ms=max(
                0,
                int(getattr(self._settings, "mainnet_codex_v1458_stup_fill_shadow_decision_latency_ms", 250) or 0),
            ),
            ttl_seconds=max(
                1,
                int(getattr(self._settings, "mainnet_codex_v1458_stup_fill_shadow_ttl_seconds", 90) or 90),
            ),
            notional_usdc=notional_usdc,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            partial_exit_pct=partial_exit_pct,
            action_id=action_id,
            variants=self._variants,
        )
        if not samples:
            return False
        self._started_groups.add(group_id)
        for sample in samples:
            sample_id = str(sample["sample_id"])
            self._samples[sample_id] = sample
            self._count_event("started", str(sample["variant"]))
            await self._repo.log_event(
                run_id,
                "adaptive_stup_fill_shadow_started",
                {
                    **sample,
                    "version": self._version,
                    "lane_code": "STUP-S",
                    "market_state": "STUP-S:clean_extension",
                    "shadow_only": True,
                    "places_real_order": False,
                },
            )
        return True

    async def update(self) -> None:
        if not self._samples:
            return
        getter = getattr(self._client, "get_agg_trades", None)
        if not callable(getter):
            await self._log_unavailable()
            return
        async with self._lock:
            groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
            for sample_id, sample in self._samples.items():
                groups.setdefault(str(sample.get("group_id") or sample_id), []).append((sample_id, sample))
            now_ms = int(time.time() * 1000)
            for group_id, grouped_samples in groups.items():
                anchor = grouped_samples[0][1]
                end_ms = min(now_ms, int(anchor.get("expires_ms") or now_ms))
                eligible_ms = int(anchor.get("eligible_after_ms") or 0)
                if end_ms < eligible_ms:
                    continue
                trades, data_complete, cursor_ms, next_from_id = await self._fetch_group(
                    group_id,
                    anchor,
                    end_ms,
                )
                if trades is None:
                    continue
                for _sample_id, sample in grouped_samples:
                    sample["cursor_ms"] = cursor_ms
                    sample["next_from_id"] = next_from_id
                for sample_id, sample in list(grouped_samples):
                    fill = first_stup_shadow_fill(sample, trades)
                    if fill is not None:
                        await self._resolve(sample_id, sample, fill)
                    elif now_ms >= int(sample.get("expires_ms") or now_ms) and data_complete:
                        await self._resolve(sample_id, sample, no_fill_outcome(sample))

    async def _fetch_group(
        self,
        group_id: str,
        anchor: Mapping[str, Any],
        end_ms: int,
    ) -> tuple[list[Mapping[str, Any]] | None, bool, int, int | None]:
        max_pages = max(
            1,
            int(getattr(self._settings, "mainnet_codex_v1458_stup_fill_shadow_max_pages", 10) or 10),
        )
        cursor_ms = int(anchor.get("cursor_ms") or anchor.get("eligible_after_ms") or 0)
        from_id_raw = anchor.get("next_from_id")
        from_id = int(from_id_raw) if from_id_raw is not None else None
        trades: list[Mapping[str, Any]] = []
        data_complete = True
        try:
            for page_index in range(max_pages):
                rows = list(
                    await self._client.get_agg_trades(
                        str(anchor.get("symbol") or "ETHUSDC"),
                        start_time=cursor_ms if from_id is None else None,
                        end_time=end_ms if from_id is None else None,
                        from_id=from_id,
                        limit=1000,
                    )
                    or []
                )
                if not rows:
                    cursor_ms = end_ms + 1
                    break
                in_window_rows = [
                    row
                    for row in rows
                    if int(row.get("T", row.get("time", end_ms + 1))) <= end_ms
                ]
                trades.extend(in_window_rows)
                crossed_end = len(in_window_rows) < len(rows)
                last = rows[-1]
                try:
                    last_id = int(last.get("a", last.get("id")))
                except (TypeError, ValueError):
                    last_id = None
                try:
                    last_ms = int(last.get("T", last.get("time")))
                except (TypeError, ValueError):
                    last_ms = cursor_ms
                cursor_ms = max(cursor_ms, last_ms + 1)
                if crossed_end:
                    cursor_ms = end_ms + 1
                    break
                if len(rows) < 1000:
                    from_id = last_id + 1 if last_id is not None else None
                    break
                if last_id is None or page_index + 1 >= max_pages:
                    data_complete = False
                    break
                from_id = last_id + 1
        except Exception as exc:  # noqa: BLE001 - telemetry cannot stop execution
            logger.warning(
                "adaptive_stup_fill_shadow_fetch_failed",
                group_id=group_id,
                error=str(exc)[:200],
            )
            return None, False, cursor_ms, from_id
        return trades, data_complete, cursor_ms, from_id

    async def _resolve(
        self,
        sample_id: str,
        sample: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> None:
        await self._repo.log_event(
            str(sample.get("run_id") or ""),
            "adaptive_stup_fill_shadow_outcome",
            {
                **sample,
                **outcome,
                "version": self._version,
                "lane_code": "STUP-S",
                "market_state": "STUP-S:clean_extension",
                "shadow_only": True,
                "places_real_order": False,
                "promotion_counts_as": "fill_diagnostic_only",
            },
        )
        self._count_event("filled" if outcome.get("filled") else "no_fill", str(sample.get("variant") or ""))
        self._samples.pop(sample_id, None)

    async def _log_unavailable(self) -> None:
        for sample in self._samples.values():
            group_id = str(sample.get("group_id") or "")
            if group_id in self._unavailable_groups:
                continue
            self._unavailable_groups.add(group_id)
            await self._repo.log_event(
                str(sample.get("run_id") or ""),
                "adaptive_stup_fill_shadow_unavailable",
                {"group_id": group_id, "reason": "client_missing_get_agg_trades"},
            )
