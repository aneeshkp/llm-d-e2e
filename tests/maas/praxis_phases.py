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
    aitenant_payload_processing_type,
    create_praxis_tenant,
    has_praxis_finalizer,
    maastenantconfig,
    payload_processing_type,
    praxis_profile_active,
    set_payload_processing_type,
    wait_for_praxis_finalizer,
)

# Values that must stay on the legacy payload-processing path and never attach
# the praxis-cleanup finalizer.
NON_PRAXIS_VALUES = ("foo", "", "ipp")


def _praxis_applies(tc: TestCase) -> bool:
    return praxis_profile_active() and _declares_model_ref(tc)


# Applied per method: a class-level mark would be inherited by every TestConformance phase.
_for_praxis = pytest.mark.applies_when.with_args(_praxis_applies)


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
