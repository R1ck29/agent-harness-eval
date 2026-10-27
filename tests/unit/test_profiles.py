from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from harness_eval.cost import CostBasis
from harness_eval.profiles import AuthKind, built_in_profiles


def test_built_in_profiles_keep_auth_and_cost_truth_separate() -> None:
    profiles = built_in_profiles()

    local = profiles["local-claude-pro"]
    bedrock = profiles["corporate-claude-bedrock"]
    codex = profiles["corporate-codex"]

    assert local.auth is AuthKind.CLAUDE_SUBSCRIPTION
    assert local.cost_basis is CostBasis.SUBSCRIPTION
    assert local.monthly_budget_usd is None
    assert local.execution_enabled
    assert local.allowed_backends == frozenset({"native", "apple-container"})

    assert bedrock.auth is AuthKind.AWS_BEDROCK
    assert bedrock.cost_basis is CostBasis.PROVIDER_BILLED
    assert bedrock.monthly_budget_usd == Decimal("1500.00")
    assert bedrock.execution_enabled
    assert bedrock.allowed_backends == frozenset({"agentcore"})

    assert codex.auth is AuthKind.CODEX_EXTERNAL
    assert codex.cost_basis is CostBasis.UNAVAILABLE
    assert codex.monthly_budget_usd == Decimal("1500.00")
    assert not codex.execution_enabled
    assert not codex.allowed_backends


def test_corporate_claude_and_codex_never_share_a_budget_account() -> None:
    profiles = built_in_profiles()

    assert (
        profiles["corporate-claude-bedrock"].budget_account_id
        != profiles["corporate-codex"].budget_account_id
    )


def test_profile_catalog_and_values_are_immutable() -> None:
    profiles = built_in_profiles()

    assert set(profiles) == {
        "local-claude-pro",
        "corporate-claude-bedrock",
        "corporate-codex",
    }
    with pytest.raises(TypeError):
        profiles["new"] = profiles["local-claude-pro"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        profiles["local-claude-pro"].key = "changed"  # type: ignore[misc]
