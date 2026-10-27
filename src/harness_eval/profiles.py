"""Built-in execution profiles with non-interchangeable billing semantics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from harness_eval.cost import CostBasis


class AuthKind(StrEnum):
    CLAUDE_SUBSCRIPTION = "claude_subscription"
    AWS_BEDROCK = "aws_bedrock"
    CODEX_EXTERNAL = "codex_external"


class AgentKind(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    key: str
    agent: AgentKind
    auth: AuthKind
    cost_basis: CostBasis
    monthly_budget_usd: Decimal | None
    budget_account_id: str | None
    default_backend: str
    allowed_backends: frozenset[str]
    execution_enabled: bool


_PROFILES: Mapping[str, ExecutionProfile] = MappingProxyType(
    {
        "local-claude-pro": ExecutionProfile(
            key="local-claude-pro",
            agent=AgentKind.CLAUDE_CODE,
            auth=AuthKind.CLAUDE_SUBSCRIPTION,
            cost_basis=CostBasis.SUBSCRIPTION,
            monthly_budget_usd=None,
            budget_account_id=None,
            default_backend="native",
            allowed_backends=frozenset({"native", "apple-container"}),
            execution_enabled=True,
        ),
        "corporate-claude-bedrock": ExecutionProfile(
            key="corporate-claude-bedrock",
            agent=AgentKind.CLAUDE_CODE,
            auth=AuthKind.AWS_BEDROCK,
            cost_basis=CostBasis.PROVIDER_BILLED,
            monthly_budget_usd=Decimal("1500.00"),
            budget_account_id="claude-code",
            default_backend="agentcore",
            allowed_backends=frozenset({"agentcore"}),
            execution_enabled=True,
        ),
        "corporate-codex": ExecutionProfile(
            key="corporate-codex",
            agent=AgentKind.CODEX,
            auth=AuthKind.CODEX_EXTERNAL,
            cost_basis=CostBasis.UNAVAILABLE,
            monthly_budget_usd=Decimal("1500.00"),
            budget_account_id="codex",
            default_backend="agentcore",
            allowed_backends=frozenset(),
            execution_enabled=False,
        ),
    }
)


def built_in_profiles() -> Mapping[str, ExecutionProfile]:
    return _PROFILES
