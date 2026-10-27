"""Fail-closed sandbox capability and trust-profile decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class Enforcement(StrEnum):
    ENFORCED = "enforced"
    NOT_ENFORCED = "not_enforced"
    UNKNOWN = "unknown"


class EvidenceSource(StrEnum):
    CALLER_DECLARED = "caller_declared"
    VERSIONED_CONTRACT = "versioned_contract"
    INDEPENDENT_PROBE = "independent_probe"


class TrustProfile(StrEnum):
    T0_STATIC_INSPECTION = "t0_static_inspection"
    T1_REVIEWED_STATIC = "t1_reviewed_static"
    T2_APPROVED_EXECUTABLE = "t2_approved_executable"
    T3_HOSTILE_FIXTURE = "t3_hostile_fixture"


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    backend: str
    ephemeral_workspace: Enforcement
    ambient_config_suppressed: Enforcement
    credential_non_visibility: Enforcement
    network_policy: Enforcement
    cancellation: Enforcement
    resource_limits: Enforcement
    offline_model: Enforcement
    evidence: str
    evidence_source: EvidenceSource

    @classmethod
    def all_enforced(
        cls,
        *,
        backend: str,
        evidence: str,
        evidence_source: EvidenceSource = EvidenceSource.CALLER_DECLARED,
    ) -> SandboxCapabilities:
        return cls(
            backend=backend,
            ephemeral_workspace=Enforcement.ENFORCED,
            ambient_config_suppressed=Enforcement.ENFORCED,
            credential_non_visibility=Enforcement.ENFORCED,
            network_policy=Enforcement.ENFORCED,
            cancellation=Enforcement.ENFORCED,
            resource_limits=Enforcement.ENFORCED,
            offline_model=Enforcement.ENFORCED,
            evidence=evidence,
            evidence_source=evidence_source,
        )

    def replace(
        self,
        *,
        network_policy: Enforcement | None = None,
        offline_model: Enforcement | None = None,
    ) -> SandboxCapabilities:
        return replace(
            self,
            network_policy=network_policy or self.network_policy,
            offline_model=offline_model or self.offline_model,
        )

    def authorizes(self, trust: TrustProfile) -> bool:
        if not isinstance(trust, TrustProfile):
            return False
        if trust is TrustProfile.T0_STATIC_INSPECTION:
            return True
        base = (
            self.ephemeral_workspace,
            self.ambient_config_suppressed,
            self.cancellation,
        )
        if trust is TrustProfile.T1_REVIEWED_STATIC:
            return all(value is Enforcement.ENFORCED for value in base)
        # Phase 0 has no trusted verifier for executable-sandbox evidence.
        if trust in {
            TrustProfile.T2_APPROVED_EXECUTABLE,
            TrustProfile.T3_HOSTILE_FIXTURE,
        }:
            return False
        return False


def native_claude_capabilities() -> SandboxCapabilities:
    return SandboxCapabilities(
        backend="native",
        ephemeral_workspace=Enforcement.ENFORCED,
        ambient_config_suppressed=Enforcement.ENFORCED,
        credential_non_visibility=Enforcement.NOT_ENFORCED,
        network_policy=Enforcement.NOT_ENFORCED,
        cancellation=Enforcement.ENFORCED,
        resource_limits=Enforcement.UNKNOWN,
        offline_model=Enforcement.NOT_ENFORCED,
        evidence="Native process retains host-user and public-network authority.",
        evidence_source=EvidenceSource.VERSIONED_CONTRACT,
    )


def apple_container_067_capabilities() -> SandboxCapabilities:
    return SandboxCapabilities(
        backend="apple-container",
        ephemeral_workspace=Enforcement.ENFORCED,
        ambient_config_suppressed=Enforcement.ENFORCED,
        credential_non_visibility=Enforcement.NOT_ENFORCED,
        network_policy=Enforcement.NOT_ENFORCED,
        cancellation=Enforcement.ENFORCED,
        resource_limits=Enforcement.ENFORCED,
        offline_model=Enforcement.NOT_ENFORCED,
        evidence="BenchFlow 0.6.7 rejects no-network Apple Container tasks.",
        evidence_source=EvidenceSource.VERSIONED_CONTRACT,
    )


def agentcore_067_capabilities() -> SandboxCapabilities:
    return SandboxCapabilities(
        backend="agentcore",
        ephemeral_workspace=Enforcement.ENFORCED,
        ambient_config_suppressed=Enforcement.ENFORCED,
        credential_non_visibility=Enforcement.NOT_ENFORCED,
        network_policy=Enforcement.NOT_ENFORCED,
        cancellation=Enforcement.ENFORCED,
        resource_limits=Enforcement.ENFORCED,
        offline_model=Enforcement.NOT_ENFORCED,
        evidence="BenchFlow 0.6.7 configures AgentCore with PUBLIC networking.",
        evidence_source=EvidenceSource.VERSIONED_CONTRACT,
    )
