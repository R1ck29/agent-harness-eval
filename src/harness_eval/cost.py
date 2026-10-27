"""Cost provenance and independent monthly budget contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_EVIDENCE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class CostBasis(StrEnum):
    """The provenance of a cost observation."""

    PROVIDER_BILLED = "provider_billed"
    CLIENT_ESTIMATE = "client_estimate"
    SUBSCRIPTION = "subscription"
    UNAVAILABLE = "unavailable"


class InvalidCostObservation(ValueError):
    """Raised when a cost value contradicts its stated provenance."""


class BudgetExceeded(ValueError):
    """Raised when a reservation would exceed one budget account."""


@dataclass(frozen=True, slots=True)
class CostEvidence:
    """Reference that binds a cost claim to retained source evidence."""

    source_ref: str
    run_id: str
    account_id: str
    billing_period: str

    def __post_init__(self) -> None:
        if not _EVIDENCE_PATTERN.fullmatch(self.source_ref):
            raise InvalidCostObservation("source_ref must be a sha256 digest")
        if not self.run_id or not self.account_id:
            raise InvalidCostObservation("run and account identifiers are required")
        if not _MONTH_PATTERN.fullmatch(self.billing_period):
            raise InvalidCostObservation("billing_period must use YYYY-MM")


@dataclass(frozen=True, slots=True)
class CostObservation:
    """A typed cost claim linked to retained evidence for later verification."""

    basis: CostBasis
    amount_usd: Decimal | None
    evidence: CostEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.basis, CostBasis):
            raise InvalidCostObservation("cost basis must be a CostBasis value")
        numeric = self.basis in {
            CostBasis.PROVIDER_BILLED,
            CostBasis.CLIENT_ESTIMATE,
        }
        if numeric:
            if not isinstance(self.amount_usd, Decimal):
                raise InvalidCostObservation(
                    f"{self.basis.value} observations require a Decimal amount"
                )
            if not self.amount_usd.is_finite() or self.amount_usd < 0:
                raise InvalidCostObservation(
                    "cost amount must be finite and non-negative"
                )
            if not isinstance(self.evidence, CostEvidence):
                raise InvalidCostObservation(
                    f"{self.basis.value} observations require cost evidence"
                )
            return
        if self.amount_usd is not None or self.evidence is not None:
            raise InvalidCostObservation(
                f"{self.basis.value} observations cannot carry cost evidence"
            )


@dataclass(frozen=True, slots=True)
class MonthlyBudget:
    """An immutable reservation view for one product and calendar month."""

    account_id: str
    month: str
    limit_usd: Decimal
    spent_usd: Decimal = Decimal("0")
    reserved_usd: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        if not _MONTH_PATTERN.fullmatch(self.month):
            raise ValueError("month must use YYYY-MM")
        values = (self.limit_usd, self.spent_usd, self.reserved_usd)
        if not all(isinstance(value, Decimal) for value in values):
            raise TypeError("budget values must use Decimal")
        if not all(value.is_finite() for value in values):
            raise ValueError("budget values must be finite")
        if self.limit_usd <= 0:
            raise ValueError("limit_usd must be positive")
        if self.spent_usd < 0 or self.reserved_usd < 0:
            raise ValueError("spent and reserved values must be non-negative")
        if self.spent_usd + self.reserved_usd > self.limit_usd:
            raise BudgetExceeded("spent plus reserved exceeds the account limit")

    @property
    def remaining_usd(self) -> Decimal:
        return self.limit_usd - self.spent_usd - self.reserved_usd

    def reserve(self, amount_usd: Decimal) -> MonthlyBudget:
        if not isinstance(amount_usd, Decimal):
            raise TypeError("reservation amount must use Decimal")
        if not amount_usd.is_finite() or amount_usd <= 0:
            raise ValueError("reservation amount must be finite and positive")
        if amount_usd > self.remaining_usd:
            raise BudgetExceeded(
                f"reservation exceeds remaining budget for {self.account_id}"
            )
        return replace(self, reserved_usd=self.reserved_usd + amount_usd)


def corporate_budgets(month: str) -> Mapping[str, MonthlyBudget]:
    """Create independent Claude Code and Codex accounts for one month."""

    limit = Decimal("1500.00")
    return MappingProxyType(
        {
            "claude-code": MonthlyBudget("claude-code", month, limit),
            "codex": MonthlyBudget("codex", month, limit),
        }
    )
