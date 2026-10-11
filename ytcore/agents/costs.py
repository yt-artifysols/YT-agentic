"""Cost metering with per-job and per-channel budgets.

Every provider call is authorised against the remaining budget BEFORE it is made (using the
provider's upper-bound estimate) and recorded AFTER it with its actual cost. The database
enforces both caps again on insert (SQLSTATE YTB01 channel/day, YTB02 job). Hitting a cap raises
BudgetExceeded and records an alert; the workflow then fails the job with that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ytcore.store.errors import BudgetExceeded, JobBudgetExceeded


@dataclass(frozen=True)
class Spend:
    job_cents: int
    job_budget_cents: int
    channel_today_cents: int
    channel_budget_cents: int


class CostLedger(Protocol):
    def record_cost(
        self,
        job_id: str,
        kind: str,
        cost_cents: int,
        *,
        provider: str,
        detail: dict[str, Any],
        actor: str,
        over_cap: bool = False,
    ) -> None:
        """Insert a cost row; raises BudgetExceeded / JobBudgetExceeded at a cap.

        over_cap=True records a cost that was already incurred past a cap (audited)."""
        ...

    def spend(self, job_id: str, channel_id: str) -> Spend: ...

    def alert(self, channel_id: str, job_id: str | None, kind: str, message: str) -> None: ...


class CostMeter:
    def __init__(self, ledger: CostLedger, job_id: str, channel_id: str, *, actor: str) -> None:
        self._ledger = ledger
        self.job_id, self.channel_id, self.actor = job_id, channel_id, actor
        self.total_cents = 0
        self.calls: list[tuple[str, str, int]] = []  # (provider, what, cents)

    def _cap_hit(self, exc: BudgetExceeded) -> BudgetExceeded:
        kind = "budget.job" if isinstance(exc, JobBudgetExceeded) else "budget.channel"
        self._ledger.alert(self.channel_id, self.job_id, kind, str(exc)[:500])
        return exc

    def authorize(self, estimate_cents: int, what: str) -> None:
        s = self._ledger.spend(self.job_id, self.channel_id)
        if s.job_cents + estimate_cents > s.job_budget_cents or s.job_cents >= s.job_budget_cents:
            raise self._cap_hit(
                JobBudgetExceeded(
                    f"job budget exceeded before {what}: {s.job_cents} + ~{estimate_cents} > "
                    f"{s.job_budget_cents} cents"
                )
            )
        if (
            s.channel_today_cents + estimate_cents > s.channel_budget_cents
            or s.channel_today_cents >= s.channel_budget_cents
        ):
            raise self._cap_hit(
                BudgetExceeded(
                    f"daily channel budget exceeded before {what}: {s.channel_today_cents} + "
                    f"~{estimate_cents} > {s.channel_budget_cents} cents"
                )
            )

    def charge(self, kind: str, cents: int, *, provider: str, what: str, **detail: Any) -> None:
        if cents <= 0:
            return
        try:
            self._ledger.record_cost(
                self.job_id,
                kind,
                cents,
                provider=provider,
                detail={"what": what, **detail},
                actor=self.actor,
            )
        except BudgetExceeded as exc:
            # The money is already spent: record it as an audited overrun so the ledger stays
            # true and every later authorize() refuses, then fail with the cap reason.
            self._ledger.record_cost(
                self.job_id,
                kind,
                cents,
                provider=provider,
                detail={"what": what, **detail},
                actor=self.actor,
                over_cap=True,
            )
            self.total_cents += cents
            raise self._cap_hit(exc) from None
        self.total_cents += cents
        self.calls.append((provider, what, cents))
