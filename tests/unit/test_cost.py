from decimal import Decimal

import pytest

from harness_eval.cost import (
    BudgetExceeded,
    CostBasis,
    CostEvidence,
    CostObservation,
    InvalidCostObservation,
    MonthlyBudget,
    corporate_budgets,
)


def _evidence() -> CostEvidence:
    return CostEvidence(
        source_ref="sha256:" + "a" * 64,
        run_id="run-1",
        account_id="claude-code",
        billing_period="2026-08",
    )


def test_subscription_and_unavailable_cost_cannot_be_encoded_as_zero() -> None:
    for basis in (CostBasis.SUBSCRIPTION, CostBasis.UNAVAILABLE):
        with pytest.raises(InvalidCostObservation):
            CostObservation(basis=basis, amount_usd=Decimal("0"))


def test_unknown_or_unparsed_cost_basis_is_rejected() -> None:
    for invalid in ("not-a-basis", CostBasis.PROVIDER_BILLED.value):
        with pytest.raises(InvalidCostObservation):
            CostObservation(basis=invalid, amount_usd=None)  # type: ignore[arg-type]


def test_provider_billed_cost_requires_a_non_negative_amount() -> None:
    with pytest.raises(InvalidCostObservation):
        CostObservation(basis=CostBasis.PROVIDER_BILLED, amount_usd=None)

    observation = CostObservation(
        basis=CostBasis.PROVIDER_BILLED,
        amount_usd=Decimal("12.34"),
        evidence=_evidence(),
    )

    assert observation.amount_usd == Decimal("12.34")

    estimate = CostObservation(
        basis=CostBasis.CLIENT_ESTIMATE,
        amount_usd=Decimal("0"),
        evidence=_evidence(),
    )
    assert estimate.amount_usd == Decimal("0")

    with pytest.raises(InvalidCostObservation):
        CostObservation(
            basis=CostBasis.CLIENT_ESTIMATE,
            amount_usd=Decimal("NaN"),
            evidence=_evidence(),
        )


def test_numeric_cost_requires_run_account_period_and_source_digest() -> None:
    with pytest.raises(InvalidCostObservation):
        CostObservation(
            basis=CostBasis.PROVIDER_BILLED,
            amount_usd=Decimal("1"),
        )
    with pytest.raises(InvalidCostObservation):
        CostEvidence(
            source_ref="invoice.csv",
            run_id="run-1",
            account_id="claude-code",
            billing_period="2026-08",
        )


def test_budget_reservation_is_immutable_and_fails_closed() -> None:
    budget = MonthlyBudget(
        account_id="claude",
        month="2026-08",
        limit_usd=Decimal("1500.00"),
    )

    reserved = budget.reserve(Decimal("1400.00"))

    assert budget.reserved_usd == Decimal("0")
    assert reserved.reserved_usd == Decimal("1400.00")
    with pytest.raises(BudgetExceeded):
        reserved.reserve(Decimal("100.01"))

    for invalid in (Decimal("0"), Decimal("-1"), Decimal("NaN")):
        with pytest.raises(ValueError):
            budget.reserve(invalid)
    with pytest.raises(TypeError):
        budget.reserve(1.0)  # type: ignore[arg-type]


def test_claude_cannot_borrow_unused_codex_budget() -> None:
    budgets = corporate_budgets(month="2026-08")
    exhausted_claude = budgets["claude-code"].reserve(Decimal("1500.00"))

    with pytest.raises(BudgetExceeded):
        exhausted_claude.reserve(Decimal("0.01"))

    assert budgets["codex"].remaining_usd == Decimal("1500.00")


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"account_id": ""}, ValueError),
        ({"month": "2026-13"}, ValueError),
        ({"limit_usd": Decimal("0")}, ValueError),
        ({"spent_usd": Decimal("-1")}, ValueError),
        ({"reserved_usd": Decimal("1500.01")}, BudgetExceeded),
        ({"limit_usd": 1500.0}, TypeError),
    ],
)
def test_invalid_budget_state_is_rejected(
    kwargs: dict[str, object],
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "account_id": "claude",
        "month": "2026-08",
        "limit_usd": Decimal("1500.00"),
    }
    values.update(kwargs)

    with pytest.raises(error):
        MonthlyBudget(**values)  # type: ignore[arg-type]
