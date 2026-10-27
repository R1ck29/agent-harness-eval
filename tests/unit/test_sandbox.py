from harness_eval.sandbox import (
    Enforcement,
    EvidenceSource,
    SandboxCapabilities,
    TrustProfile,
    agentcore_067_capabilities,
    apple_container_067_capabilities,
    native_claude_capabilities,
)


def test_local_backends_allow_reviewed_static_bundles_but_not_executables() -> None:
    for capabilities in (
        native_claude_capabilities(),
        apple_container_067_capabilities(),
        agentcore_067_capabilities(),
    ):
        assert capabilities.authorizes(TrustProfile.T1_REVIEWED_STATIC)
        assert not capabilities.authorizes(TrustProfile.T2_APPROVED_EXECUTABLE)


def test_unknown_capability_fails_closed_for_executable_harnesses() -> None:
    capabilities = SandboxCapabilities.all_enforced(
        backend="candidate",
        evidence="test fixture",
    ).replace(network_policy=Enforcement.UNKNOWN)

    assert not capabilities.authorizes(TrustProfile.T2_APPROVED_EXECUTABLE)


def test_stock_agentcore_records_public_network_gap() -> None:
    capabilities = agentcore_067_capabilities()

    assert capabilities.network_policy is Enforcement.NOT_ENFORCED
    assert "PUBLIC" in capabilities.evidence


def test_t0_is_always_safe_because_it_executes_nothing() -> None:
    assert native_claude_capabilities().authorizes(TrustProfile.T0_STATIC_INSPECTION)


def test_phase_zero_never_authorizes_t3_without_a_trusted_verifier() -> None:
    capabilities = SandboxCapabilities.all_enforced(
        backend="attack-fixture",
        evidence="independent probe",
        evidence_source=EvidenceSource.INDEPENDENT_PROBE,
    )

    assert not capabilities.authorizes(TrustProfile.T3_HOSTILE_FIXTURE)
    assert not capabilities.replace(offline_model=Enforcement.UNKNOWN).authorizes(
        TrustProfile.T3_HOSTILE_FIXTURE
    )


def test_caller_declared_capabilities_cannot_self_authorize_t2() -> None:
    claimed = SandboxCapabilities.all_enforced(
        backend="self-declared",
        evidence="trust me",
    )
    observed = SandboxCapabilities.all_enforced(
        backend="observed",
        evidence="probe artifact sha256:abc",
        evidence_source=EvidenceSource.INDEPENDENT_PROBE,
    )

    assert not claimed.authorizes(TrustProfile.T2_APPROVED_EXECUTABLE)
    assert not observed.authorizes(TrustProfile.T2_APPROVED_EXECUTABLE)


def test_unknown_trust_value_never_falls_through_to_t3() -> None:
    capabilities = SandboxCapabilities.all_enforced(
        backend="observed",
        evidence="independent probe",
        evidence_source=EvidenceSource.INDEPENDENT_PROBE,
    )

    for invalid in ("unexpected", None, 0):
        assert not capabilities.authorizes(invalid)  # type: ignore[arg-type]
