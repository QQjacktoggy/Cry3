"""Durable, observational result persistence for the v1.4.59 evidence layer.

This module only writes the v1.4.59 result tables created by migration 006.
It deliberately has no application, exchange, order, or Telegram dependency.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from typing import Any

from src.gridbot.storage.database import Database


class AdaptiveResultRepository:
    """Idempotent storage for immutable shadow and reconciliation results."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _text(payload: Mapping[str, Any], key: str, *, nullable: bool = False) -> str | None:
        value = payload.get(key)
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not value.strip():
            suffix = " or None" if nullable else ""
            raise ValueError(f"{key} must be a non-empty string{suffix}")
        return value

    @staticmethod
    def _integer(value: Any, key: str, *, nullable: bool = False) -> int | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            suffix = " or None" if nullable else ""
            raise ValueError(f"{key} must be an integer{suffix}")
        return value

    @staticmethod
    def _nonnegative_integer(value: Any, key: str, *, nullable: bool = False) -> int | None:
        normalized = AdaptiveResultRepository._integer(value, key, nullable=nullable)
        if normalized is not None and normalized < 0:
            raise ValueError(f"{key} must be non-negative")
        return normalized

    @staticmethod
    def _number(value: Any, key: str, *, nullable: bool = False) -> float | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            suffix = " or None" if nullable else ""
            raise ValueError(f"{key} must be a finite number{suffix}")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{key} must be a finite number")
        return normalized

    @staticmethod
    def _flag(value: Any, key: str, *, nullable: bool = False) -> int | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int) and value in (0, 1):
            return value
        suffix = " or None" if nullable else ""
        raise ValueError(f"{key} must be a boolean or 0/1{suffix}")

    @staticmethod
    def _json(value: Any, key: str) -> str:
        if not isinstance(value, Mapping):
            raise ValueError(f"{key} must be a mapping")
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be JSON-serializable") from exc

    @staticmethod
    def _optional_json(value: Any, key: str) -> str:
        return AdaptiveResultRepository._json({} if value is None else value, key)

    @staticmethod
    def _query_row_dicts(rows: Sequence[Any]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def _shadow_values(self, evaluation: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[Any, ...]]:
        if not isinstance(evaluation, Mapping):
            raise ValueError("evaluation must be a mapping")
        now_ms = int(time.time() * 1000)
        required_text = (
            "session_id", "opportunity_id", "variant", "fill_model", "simulation_version",
            "fill_status", "data_quality",
        )
        values: dict[str, Any] = {key: self._text(evaluation, key) for key in required_text}
        values.update(
            entry_offset_bp=self._number(evaluation.get("entry_offset_bp"), "entry_offset_bp"),
            entry_limit_price=self._number(evaluation.get("entry_limit_price"), "entry_limit_price", nullable=True),
            decision_latency_ms=self._nonnegative_integer(
                evaluation.get("decision_latency_ms"), "decision_latency_ms"
            ),
            entry_ttl_ms=self._nonnegative_integer(evaluation.get("entry_ttl_ms"), "entry_ttl_ms"),
            filled_qty=self._number(evaluation.get("filled_qty", 0), "filled_qty"),
            avg_fill_price=self._number(evaluation.get("avg_fill_price"), "avg_fill_price", nullable=True),
            first_fill_at_ms=self._nonnegative_integer(
                evaluation.get("first_fill_at_ms"), "first_fill_at_ms", nullable=True
            ),
            fill_age_ms=self._nonnegative_integer(evaluation.get("fill_age_ms"), "fill_age_ms", nullable=True),
            partial_fill_ratio=self._number(evaluation.get("partial_fill_ratio", 0), "partial_fill_ratio"),
            tp_anchor=self._text(evaluation, "tp_anchor", nullable=True),
            tp_bp=self._number(evaluation.get("tp_bp"), "tp_bp", nullable=True),
            sl_anchor=self._text(evaluation, "sl_anchor", nullable=True),
            sl_bp=self._number(evaluation.get("sl_bp"), "sl_bp", nullable=True),
            max_hold_ms=self._nonnegative_integer(
                evaluation.get("max_hold_ms"), "max_hold_ms", nullable=True
            ),
            mfe_bp=self._number(evaluation.get("mfe_bp"), "mfe_bp", nullable=True),
            mae_bp=self._number(evaluation.get("mae_bp"), "mae_bp", nullable=True),
            exit_at_ms=self._nonnegative_integer(
                evaluation.get("exit_at_ms"), "exit_at_ms", nullable=True
            ),
            exit_price=self._number(evaluation.get("exit_price"), "exit_price", nullable=True),
            exit_reason=self._text(evaluation, "exit_reason", nullable=True),
            gross_pnl_usdc=self._number(
                evaluation.get("gross_pnl_usdc"), "gross_pnl_usdc", nullable=True
            ),
            commission_usdc=self._number(
                evaluation.get("commission_usdc"), "commission_usdc", nullable=True
            ),
            funding_usdc=self._number(evaluation.get("funding_usdc"), "funding_usdc", nullable=True),
            net_pnl_usdc=self._number(evaluation.get("net_pnl_usdc"), "net_pnl_usdc", nullable=True),
            ambiguous_touch=self._flag(evaluation.get("ambiguous_touch", False), "ambiguous_touch"),
            input_json=self._optional_json(evaluation.get("input"), "input"),
            recorded_at_ms=self._nonnegative_integer(
                evaluation.get("recorded_at_ms", now_ms), "recorded_at_ms"
            ),
        )
        ordered_keys = (
            "session_id", "opportunity_id", "variant", "fill_model", "simulation_version",
            "entry_offset_bp", "entry_limit_price", "decision_latency_ms", "entry_ttl_ms",
            "fill_status", "filled_qty", "avg_fill_price", "first_fill_at_ms", "fill_age_ms",
            "partial_fill_ratio", "tp_anchor", "tp_bp", "sl_anchor", "sl_bp", "max_hold_ms",
            "mfe_bp", "mae_bp", "exit_at_ms", "exit_price", "exit_reason", "gross_pnl_usdc",
            "commission_usdc", "funding_usdc", "net_pnl_usdc", "data_quality", "ambiguous_touch",
            "input_json", "recorded_at_ms",
        )
        return values, tuple(values[key] for key in ordered_keys)

    async def record_shadow_evaluation(self, evaluation: Mapping[str, Any]) -> bool:
        """Insert one immutable shadow result.

        An exact retry returns ``False``. A payload that reuses the immutable
        primary key with different evidence raises and leaves the original row
        untouched.
        """
        values, params = self._shadow_values(evaluation)
        existing = await self.get_shadow_evaluation(
            values["session_id"], values["opportunity_id"], values["variant"],
            values["fill_model"], values["simulation_version"],
        )
        compare_keys = (
            "session_id", "opportunity_id", "variant", "fill_model", "simulation_version",
            "entry_offset_bp", "entry_limit_price", "decision_latency_ms", "entry_ttl_ms",
            "fill_status", "filled_qty", "avg_fill_price", "first_fill_at_ms", "fill_age_ms",
            "partial_fill_ratio", "tp_anchor", "tp_bp", "sl_anchor", "sl_bp", "max_hold_ms",
            "mfe_bp", "mae_bp", "exit_at_ms", "exit_price", "exit_reason", "gross_pnl_usdc",
            "commission_usdc", "funding_usdc", "net_pnl_usdc", "data_quality", "ambiguous_touch",
            "input_json",
        )
        if existing is not None:
            if all(existing[key] == values[key] for key in compare_keys):
                return False
            raise ValueError("conflicting immutable shadow evaluation")
        cursor = await self._db.execute(
            """INSERT INTO shadow_evaluations (
                session_id, opportunity_id, variant, fill_model, simulation_version,
                entry_offset_bp, entry_limit_price, decision_latency_ms, entry_ttl_ms,
                fill_status, filled_qty, avg_fill_price, first_fill_at_ms, fill_age_ms,
                partial_fill_ratio, tp_anchor, tp_bp, sl_anchor, sl_bp, max_hold_ms,
                mfe_bp, mae_bp, exit_at_ms, exit_price, exit_reason, gross_pnl_usdc,
                commission_usdc, funding_usdc, net_pnl_usdc, data_quality,
                ambiguous_touch, input_json, recorded_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
        return cursor.rowcount == 1

    async def get_shadow_evaluation(
        self,
        session_id: str,
        opportunity_id: str,
        variant: str,
        fill_model: str,
        simulation_version: str,
    ) -> dict[str, Any] | None:
        keys = ("session_id", "opportunity_id", "variant", "fill_model", "simulation_version")
        data = dict(zip(keys, (session_id, opportunity_id, variant, fill_model, simulation_version)))
        for key in keys:
            self._text(data, key)
        return await self._db.fetchone(
            """SELECT * FROM shadow_evaluations
            WHERE session_id = ? AND opportunity_id = ? AND variant = ?
              AND fill_model = ? AND simulation_version = ?""",
            (session_id, opportunity_id, variant, fill_model, simulation_version),
        )

    async def list_shadow_evaluations(
        self,
        session_id: str,
        *,
        complete_only: bool = False,
        data_quality: str | None = None,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        self._text({"session_id": session_id}, "session_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer from 1 to 10000")
        if complete_only and data_quality not in (None, "COMPLETE"):
            raise ValueError("complete_only conflicts with a non-COMPLETE data_quality")
        selected_quality = "COMPLETE" if complete_only else data_quality
        if selected_quality is not None:
            self._text({"data_quality": selected_quality}, "data_quality")
            return await self._db.fetchall(
                """SELECT * FROM shadow_evaluations
                WHERE session_id = ? AND data_quality = ?
                ORDER BY recorded_at_ms, opportunity_id, variant, fill_model, simulation_version LIMIT ?""",
                (session_id, selected_quality, limit),
            )
        return await self._db.fetchall(
            """SELECT * FROM shadow_evaluations
            WHERE session_id = ?
            ORDER BY recorded_at_ms, opportunity_id, variant, fill_model, simulation_version LIMIT ?""",
            (session_id, limit),
        )

    def _reconciliation_parent_values(self, reconciliation: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(reconciliation, Mapping):
            raise ValueError("reconciliation must be a mapping")
        required_text = (
            "run_id", "environment", "account_fingerprint", "symbol", "reconciliation_status",
        )
        values: dict[str, Any] = {key: self._text(reconciliation, key) for key in required_text}
        if values["reconciliation_status"] not in {"COMPLETE", "DATA_INCOMPLETE"}:
            raise ValueError("reconciliation_status must be COMPLETE or DATA_INCOMPLETE")
        now_ms = int(time.time() * 1000)
        values.update(
            reconciliation_revision=self._nonnegative_integer(
                reconciliation.get("reconciliation_revision"), "reconciliation_revision"
            ),
            completeness_reason=self._text(reconciliation, "completeness_reason", nullable=True),
            gross_realized_pnl_usdc=self._number(
                reconciliation.get("gross_realized_pnl_usdc", 0), "gross_realized_pnl_usdc"
            ),
            commission_usdc=self._number(reconciliation.get("commission_usdc"), "commission_usdc", nullable=True),
            funding_usdc=self._number(reconciliation.get("funding_usdc"), "funding_usdc", nullable=True),
            net_pnl_usdc=self._number(reconciliation.get("net_pnl_usdc"), "net_pnl_usdc", nullable=True),
            entry_maker_fills=self._nonnegative_integer(
                reconciliation.get("entry_maker_fills", 0), "entry_maker_fills"
            ),
            entry_taker_fills=self._nonnegative_integer(
                reconciliation.get("entry_taker_fills", 0), "entry_taker_fills"
            ),
            exit_maker_fills=self._nonnegative_integer(
                reconciliation.get("exit_maker_fills", 0), "exit_maker_fills"
            ),
            exit_taker_fills=self._nonnegative_integer(
                reconciliation.get("exit_taker_fills", 0), "exit_taker_fills"
            ),
            source_json=self._optional_json(reconciliation.get("source"), "source"),
            reconciled_at_ms=self._nonnegative_integer(
                reconciliation.get("reconciled_at_ms", now_ms), "reconciled_at_ms"
            ),
        )
        return values

    def _trade_values(self, parent: Mapping[str, Any], trade: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(trade, Mapping):
            raise ValueError("trade must be a mapping")
        return {
            "environment": parent["environment"],
            "account_fingerprint": parent["account_fingerprint"],
            "exchange_trade_id": self._text(trade, "exchange_trade_id"),
            "run_id": parent["run_id"],
            "reconciliation_revision": parent["reconciliation_revision"],
            "order_id": self._text(trade, "order_id", nullable=True),
            "role": self._text(trade, "role"),
            "is_maker": self._flag(trade.get("is_maker"), "is_maker", nullable=True),
            "realized_pnl_usdc": self._number(
                trade.get("realized_pnl_usdc"), "realized_pnl_usdc", nullable=True
            ),
            "commission_amount": self._number(
                trade.get("commission_amount"), "commission_amount", nullable=True
            ),
            "commission_asset": self._text(trade, "commission_asset", nullable=True),
            "commission_usdc": self._number(trade.get("commission_usdc"), "commission_usdc", nullable=True),
            "source_json": self._optional_json(trade.get("source"), "trade.source"),
        }

    def _income_values(self, parent: Mapping[str, Any], income: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(income, Mapping):
            raise ValueError("income must be a mapping")
        return {
            "environment": parent["environment"],
            "account_fingerprint": parent["account_fingerprint"],
            "exchange_income_id": self._text(income, "exchange_income_id"),
            "run_id": parent["run_id"],
            "reconciliation_revision": parent["reconciliation_revision"],
            "income_type": self._text(income, "income_type"),
            "amount": self._number(income.get("amount"), "amount"),
            "asset": self._text(income, "asset"),
            "amount_usdc": self._number(income.get("amount_usdc"), "amount_usdc", nullable=True),
            "source_json": self._optional_json(income.get("source"), "income.source"),
        }

    @staticmethod
    def _row_matches(row: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        return all(row.get(key) == value for key, value in expected.items())

    async def _fetch_transaction_rows(
        self, run_id: str, revision: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        parent_rows = await self._db.conn.execute_fetchall(
            """SELECT * FROM run_reconciliations
            WHERE run_id = ? AND reconciliation_revision = ?""",
            (run_id, revision),
        )
        parent = dict(parent_rows[0]) if parent_rows else None
        trade_rows = await self._db.conn.execute_fetchall(
            """SELECT * FROM run_reconciliation_exchange_trades
            WHERE run_id = ? AND reconciliation_revision = ?
            ORDER BY exchange_trade_id""",
            (run_id, revision),
        )
        income_rows = await self._db.conn.execute_fetchall(
            """SELECT * FROM run_reconciliation_exchange_income
            WHERE run_id = ? AND reconciliation_revision = ?
            ORDER BY exchange_income_id""",
            (run_id, revision),
        )
        return parent, self._query_row_dicts(trade_rows), self._query_row_dicts(income_rows)

    async def record_reconciliation(
        self,
        reconciliation: Mapping[str, Any],
        *,
        trades: Sequence[Mapping[str, Any]] = (),
        incomes: Sequence[Mapping[str, Any]] = (),
    ) -> bool:
        """Atomically persist one revision plus its exchange evidence.

        All parent and child rows are written in one ``BEGIN IMMEDIATE``
        transaction. An exact retry is a no-op; a conflicting same revision or
        globally reused exchange ID raises and rolls back every new row.
        """
        parent = self._reconciliation_parent_values(reconciliation)
        if isinstance(trades, (str, bytes)) or not isinstance(trades, Sequence):
            raise ValueError("trades must be a sequence of mappings")
        if isinstance(incomes, (str, bytes)) or not isinstance(incomes, Sequence):
            raise ValueError("incomes must be a sequence of mappings")
        trade_values = [self._trade_values(parent, trade) for trade in trades]
        income_values = [self._income_values(parent, income) for income in incomes]
        trade_ids = [row["exchange_trade_id"] for row in trade_values]
        income_ids = [row["exchange_income_id"] for row in income_values]
        if len(trade_ids) != len(set(trade_ids)):
            raise ValueError("duplicate exchange_trade_id in reconciliation payload")
        if len(income_ids) != len(set(income_ids)):
            raise ValueError("duplicate exchange_income_id in reconciliation payload")

        connection = self._db.conn
        began = False
        try:
            await connection.execute("BEGIN IMMEDIATE")
            began = True
            existing_parent, existing_trades, existing_incomes = await self._fetch_transaction_rows(
                parent["run_id"], parent["reconciliation_revision"]
            )
            if existing_parent is not None:
                expected_trades = sorted(trade_values, key=lambda row: row["exchange_trade_id"])
                expected_incomes = sorted(income_values, key=lambda row: row["exchange_income_id"])
                is_exact = (
                    self._row_matches(existing_parent, parent)
                    and len(existing_trades) == len(expected_trades)
                    and len(existing_incomes) == len(expected_incomes)
                    and all(self._row_matches(actual, expected) for actual, expected in zip(existing_trades, expected_trades))
                    and all(self._row_matches(actual, expected) for actual, expected in zip(existing_incomes, expected_incomes))
                )
                if not is_exact:
                    raise ValueError("conflicting reconciliation revision")
                await connection.rollback()
                began = False
                return False

            parent_keys = (
                "run_id", "reconciliation_revision", "environment", "account_fingerprint", "symbol",
                "reconciliation_status", "completeness_reason", "gross_realized_pnl_usdc", "commission_usdc",
                "funding_usdc", "net_pnl_usdc", "entry_maker_fills", "entry_taker_fills",
                "exit_maker_fills", "exit_taker_fills", "source_json", "reconciled_at_ms",
            )
            await connection.execute(
                f"INSERT INTO run_reconciliations ({', '.join(parent_keys)}) VALUES ({', '.join('?' for _ in parent_keys)})",
                tuple(parent[key] for key in parent_keys),
            )
            trade_keys = (
                "environment", "account_fingerprint", "exchange_trade_id", "run_id", "reconciliation_revision",
                "order_id", "role", "is_maker", "realized_pnl_usdc", "commission_amount", "commission_asset",
                "commission_usdc", "source_json",
            )
            for row in trade_values:
                await connection.execute(
                    f"INSERT INTO run_reconciliation_exchange_trades ({', '.join(trade_keys)}) VALUES ({', '.join('?' for _ in trade_keys)})",
                    tuple(row[key] for key in trade_keys),
                )
            income_keys = (
                "environment", "account_fingerprint", "exchange_income_id", "run_id", "reconciliation_revision",
                "income_type", "amount", "asset", "amount_usdc", "source_json",
            )
            for row in income_values:
                await connection.execute(
                    f"INSERT INTO run_reconciliation_exchange_income ({', '.join(income_keys)}) VALUES ({', '.join('?' for _ in income_keys)})",
                    tuple(row[key] for key in income_keys),
                )
            await connection.commit()
            began = False
            return True
        except Exception:
            if began:
                await connection.rollback()
            raise

    async def get_reconciliation(self, run_id: str, reconciliation_revision: int) -> dict[str, Any] | None:
        self._text({"run_id": run_id}, "run_id")
        self._nonnegative_integer(reconciliation_revision, "reconciliation_revision")
        return await self._db.fetchone(
            """SELECT * FROM run_reconciliations
            WHERE run_id = ? AND reconciliation_revision = ?""",
            (run_id, reconciliation_revision),
        )

    async def get_reconciliation_trades(
        self, run_id: str, reconciliation_revision: int
    ) -> list[dict[str, Any]]:
        """Return evidence accumulated through an append-only revision.

        Exchange trade IDs are globally unique, so a later reconciliation
        revision records only newly discovered fills and inherits all earlier
        evidence for the same run.
        """
        self._text({"run_id": run_id}, "run_id")
        self._nonnegative_integer(reconciliation_revision, "reconciliation_revision")
        return await self._db.fetchall(
            """SELECT * FROM run_reconciliation_exchange_trades
            WHERE run_id = ? AND reconciliation_revision <= ? ORDER BY exchange_trade_id""",
            (run_id, reconciliation_revision),
        )

    async def get_reconciliation_incomes(
        self, run_id: str, reconciliation_revision: int
    ) -> list[dict[str, Any]]:
        """Return income evidence accumulated through an append-only revision."""
        self._text({"run_id": run_id}, "run_id")
        self._nonnegative_integer(reconciliation_revision, "reconciliation_revision")
        return await self._db.fetchall(
            """SELECT * FROM run_reconciliation_exchange_income
            WHERE run_id = ? AND reconciliation_revision <= ? ORDER BY exchange_income_id""",
            (run_id, reconciliation_revision),
        )

    async def list_reconciliations(
        self,
        *,
        environment: str,
        account_fingerprint: str,
        symbol: str,
        complete_only: bool = False,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        scope = {
            "environment": environment,
            "account_fingerprint": account_fingerprint,
            "symbol": symbol,
        }
        for key in scope:
            self._text(scope, key)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer from 1 to 10000")
        suffix = " AND reconciliation_status = 'COMPLETE'" if complete_only else ""
        return await self._db.fetchall(
            f"""SELECT * FROM run_reconciliations
            WHERE environment = ? AND account_fingerprint = ? AND symbol = ?{suffix}
            ORDER BY reconciled_at_ms, run_id, reconciliation_revision LIMIT ?""",
            (environment, account_fingerprint, symbol, limit),
        )
