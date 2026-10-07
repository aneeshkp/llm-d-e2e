"""Praxis 3.6-only conformance phases.

``PraxisPhases`` is mixed into ``TestConformance`` (tests/test_conformance.py) after
``MaaSPhases``. Phases are deselected at collection unless the run resolves the
``maas-3.6`` profile (recorded at configure time by conftest) and the testcase
manifest declares a ``MaaSModelRef``. Each phase provisions its own test-owned
AITenant and bootstrap Gateway via ``maas.praxis`` and never touches the shared
gateway, its Service, or the default tenant.

To add a Praxis check, add a ``test_30<letter>_maas_praxis_<name>`` method here.
"""

from __future__ import annotations

import pytest

from conformance.config import TestCase
from conformance.deployer import Deployer

from maas.phases import _declares_model_ref
from maas.praxis import (
    PRAXIS_VALUE,
    GATEWAY_NAMESPACE,
    SHARED_GATEWAY_NAME,
    SHARED_GATEWAY_SERVICE_NAME,
    DEFAULT_TENANT_NAMESPACE,
    TENANT_CONFIG_NAME,
    aitenant_payload_processing_type,
    create_praxis_tenant,
    get_resource,
    has_praxis_finalizer,
    maastenantconfig,
    migrate_tenant_to_praxis,
    payload_processing_type,
    praxis_profile_active,
    restore_tenant_to_legacy,
    set_payload_processing_type,
    wait_for_praxis_finalizer,
    wait_maas_api_available,
    wait_maastenantconfig_ready,
)

# Values that must stay on the legacy payload-processing path and never attach
# the praxis-cleanup finalizer.
NON_PRAXIS_VALUES = ("foo", "", "ipp")


def _praxis_applies(tc: TestCase) -> bool:
    return praxis_profile_active() and _declares_model_ref(tc)


# Applied per method: a class-level mark would be inherited by every TestConformance phase.
_for_praxis = pytest.mark.applies_when.with_args(_praxis_applies)


def _capture_shared_state(kubectl) -> dict:
    """Snapshot the shared gateway, its Service, and the default tenant (read-only)."""
    gateway = get_resource(kubectl, "gateway", SHARED_GATEWAY_NAME, GATEWAY_NAMESPACE) or {}
    service = get_resource(kubectl, "service", SHARED_GATEWAY_SERVICE_NAME, GATEWAY_NAMESPACE) or {}
    default_tenant = get_resource(kubectl, "maastenantconfigs", TENANT_CONFIG_NAME, DEFAULT_TENANT_NAMESPACE) or {}
    return {
        "gateway_spec": gateway.get("spec"),
        "gateway_uid": (gateway.get("metadata") or {}).get("uid"),
        "service_type": (service.get("spec") or {}).get("type"),
        "default_tenant_annotations": dict((default_tenant.get("metadata") or {}).get("annotations") or {}),
        "default_tenant_uid": (default_tenant.get("metadata") or {}).get("uid"),
    }


def _assert_shared_state_unchanged(kubectl, expected: dict) -> None:
    """Fail when anything the run must never disturb changed on the shared stack."""
    observed = _capture_shared_state(kubectl)
    assert observed == expected, (
        f"shared resources changed during the Praxis phases: expected {expected!r}, observed {observed!r}"
    )


def _rollback_to_legacy_best_effort(kubectl, tenant) -> None:
    """Best-effort return to the legacy path so teardown starts from a known state."""
    try:
        config = maastenantconfig(kubectl, tenant)
    except RuntimeError:
        return
    if payload_processing_type(config) is None:
        return
    try:
        restore_tenant_to_legacy(kubectl, tenant, timeout=60, poll_interval=5)
    except (RuntimeError, TimeoutError, AssertionError) as error:
        print(f"  → best-effort praxis rollback failed (teardown will force): {error}")


class PraxisPhases:
    """3.6-only Praxis payload-processing checks, mixed into TestConformance."""

    @_for_praxis
    def test_30p_maas_praxis_annotation_contract(self, deployer: Deployer, tc: TestCase):
        """Praxis is opt-in on MaasTenantConfig: ``praxis`` attaches the cleanup finalizer, other values do not."""
        kubectl = deployer.kubectl
        with create_praxis_tenant(deployer) as tenant:
            # a fresh tenant is on the legacy path: no annotation, no praxis finalizer
            config = maastenantconfig(kubectl, tenant)
            assert payload_processing_type(config) is None, (
                "a fresh tenant must not carry the payload-processing-type annotation"
            )
            assert not has_praxis_finalizer(config), "a fresh tenant must not be on the praxis cleanup path"

            # opting in: the annotation lands on MaasTenantConfig and the controller
            # attaches its cleanup finalizer; the AITenant itself stays unannotated
            set_payload_processing_type(kubectl, tenant, PRAXIS_VALUE)
            wait_for_praxis_finalizer(kubectl, tenant, present=True)
            config = maastenantconfig(kubectl, tenant)
            assert payload_processing_type(config) == PRAXIS_VALUE
            assert aitenant_payload_processing_type(kubectl, tenant) is None, (
                "the AITenant must not mirror the MaasTenantConfig opt-in"
            )

        for value in NON_PRAXIS_VALUES:
            with create_praxis_tenant(deployer) as tenant:
                set_payload_processing_type(kubectl, tenant, value)
                config = maastenantconfig(kubectl, tenant)
                assert payload_processing_type(config) == value, (
                    f"annotation value {value!r} must be stored on MaasTenantConfig"
                )
                assert not has_praxis_finalizer(config), (
                    f"non-praxis value {value!r} must not attach the praxis-cleanup finalizer"
                )
                assert aitenant_payload_processing_type(kubectl, tenant) is None

    @_for_praxis
    def test_30q_maas_praxis_legacy_ipp_roundtrip(self, deployer: Deployer, tc: TestCase):
        """Legacy IPP ⇄ Praxis roundtrip on a test-owned tenant: handoff, readiness, rollback."""
        kubectl = deployer.kubectl
        shared_state = _capture_shared_state(kubectl)
        with create_praxis_tenant(deployer) as tenant:
            try:
                migrate_tenant_to_praxis(kubectl, tenant)
                wait_maastenantconfig_ready(kubectl, tenant)
                wait_maas_api_available(kubectl, tenant)
                restore_tenant_to_legacy(kubectl, tenant)
            finally:
                # keep teardown on the legacy path even when the roundtrip failed midway
                _rollback_to_legacy_best_effort(kubectl, tenant)
        _assert_shared_state_unchanged(kubectl, shared_state)
