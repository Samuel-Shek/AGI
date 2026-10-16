"""Integer-unit, persistent reservation ledger in the existing control DB.

No budget is configured by default. Configuring accounting never enables a
model/provider capability. Values are approved billing units, not inferred RMB.
"""
from __future__ import annotations

from contextlib import contextmanager
import time

from .policy import GateDenied


class BudgetLedger:
    def __init__(self, conn):
        self.conn = conn
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ceo_budget_windows(
          id TEXT PRIMARY KEY, grant_ref TEXT NOT NULL, unit TEXT NOT NULL,
          cap INTEGER NOT NULL CHECK(cap>=0), expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ceo_budget_reservations(
          id TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES ceo_budget_windows(id),
          reserved INTEGER NOT NULL CHECK(reserved>=0), actual INTEGER,
          state TEXT NOT NULL CHECK(state IN ('reserved','settled','unknown','cancelled')));
        """)

    @contextmanager
    def tx(self):
        if self.conn.in_transaction:
            raise RuntimeError("budget writes require an independent controller transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    @staticmethod
    def amount(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("budget amount must be nonnegative integer billing units")
        return value

    def configure(self, window_id, grant_ref, unit, cap, expires_at):
        """Trusted local operator only, immutable window; not a web/model API."""
        self.amount(cap)
        if not grant_ref or not unit or expires_at <= time.time():
            raise ValueError("explicit grant, billing unit and future expiry required")
        with self.tx():
            self.conn.execute("INSERT INTO ceo_budget_windows VALUES(?,?,?,?,?)",
                              (window_id, grant_ref, unit, cap, expires_at))

    def reserve(self, window_id, request_id, amount):
        self.amount(amount)
        with self.tx():
            previous = self.conn.execute("SELECT * FROM ceo_budget_reservations WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["window_id"] != window_id or previous["reserved"] != amount:
                    raise GateDenied("reservation_conflict", "同一请求不能变更预算含义。")
                return dict(previous)
            window = self.conn.execute("SELECT * FROM ceo_budget_windows WHERE id=?", (window_id,)).fetchone()
            if not window or window["expires_at"] <= time.time():
                raise GateDenied("budget_unapproved", "预算未获准或已过期。")
            used = self.conn.execute("""SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual
                WHEN state IN ('reserved','unknown') THEN reserved ELSE 0 END),0)
                FROM ceo_budget_reservations WHERE window_id=?""", (window_id,)).fetchone()[0]
            if used + amount > window["cap"]:
                raise GateDenied("budget_exhausted", "预算不足；没有发起调用或切换通道。")
            self.conn.execute("INSERT INTO ceo_budget_reservations VALUES(?,?,?,NULL,'reserved')",
                              (request_id, window_id, amount))
            return {"id": request_id, "window_id": window_id, "reserved": amount,
                    "actual": None, "state": "reserved"}

    def settle(self, request_id, actual):
        self.amount(actual)
        with self.tx():
            row = self.conn.execute("SELECT * FROM ceo_budget_reservations WHERE id=?", (request_id,)).fetchone()
            if not row or row["state"] not in {"reserved", "unknown", "settled"}:
                raise GateDenied("reservation_missing", "不存在可结算的预算预留。")
            if row["state"] == "settled":
                if row["actual"] != actual:
                    raise GateDenied("settlement_conflict", "已结算回执不能改写。")
                return
            # Preserve real overspend if a provider exceeds its reservation.
            # Never fabricate a lower cost to appear within budget.
            self.conn.execute("UPDATE ceo_budget_reservations SET actual=?,state='settled' WHERE id=?",
                              (actual, request_id))

    def mark_unknown(self, request_id):
        with self.tx():
            self.conn.execute("UPDATE ceo_budget_reservations SET state='unknown' WHERE id=? AND state='reserved'",
                              (request_id,))

    def cancel_unstarted(self, request_id):
        with self.tx():
            row = self.conn.execute("SELECT state FROM ceo_budget_reservations WHERE id=?", (request_id,)).fetchone()
            if not row or row[0] != "reserved":
                raise GateDenied("cannot_release_unknown", "结果未知的调用不能释放预算或盲目重试。")
            self.conn.execute("UPDATE ceo_budget_reservations SET state='cancelled' WHERE id=?", (request_id,))
