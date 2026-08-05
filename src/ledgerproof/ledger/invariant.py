"""The global money-conservation invariant.

Contract: for each currency, Σ balances of debit-normal accounts must equal
Σ balances of credit-normal accounts. A mismatch means the system created or
destroyed money out of nothing. `check(conn) -> InvariantResult` returns, per
currency, both sums, the difference, and a boolean; it is asserted in every
test and by the reconciler, and its raw output is what harness runs record in
artifacts/. Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from ledgerproof.ledger.balances import _connection


@dataclass(frozen=True)
class CurrencyCheck:
    currency: str
    sum_debit_normal: int
    sum_credit_normal: int

    @property
    def difference(self) -> int:
        return self.sum_debit_normal - self.sum_credit_normal

    @property
    def ok(self) -> bool:
        return self.difference == 0


@dataclass(frozen=True)
class InvariantResult:
    per_currency: tuple[CurrencyCheck, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.per_currency)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "per_currency": [
                {
                    "currency": c.currency,
                    "sum_debit_normal": c.sum_debit_normal,
                    "sum_credit_normal": c.sum_credit_normal,
                    "difference": c.difference,
                    "ok": c.ok,
                }
                for c in self.per_currency
            ],
        }


def check(db_url_or_conn: str | psycopg.Connection) -> InvariantResult:
    with _connection(db_url_or_conn) as conn:
        rows = conn.execute(
            "SELECT currency,"
            "       COALESCE(SUM(CASE WHEN normal = 'debit'  THEN balance_minor ELSE 0 END), 0),"
            "       COALESCE(SUM(CASE WHEN normal = 'credit' THEN balance_minor ELSE 0 END), 0)"
            "  FROM account_balances"
            " GROUP BY currency"
            " ORDER BY currency"
        ).fetchall()
    return InvariantResult(
        per_currency=tuple(
            CurrencyCheck(currency=cur, sum_debit_normal=int(dr), sum_credit_normal=int(cr))
            for cur, dr, cr in rows
        )
    )
