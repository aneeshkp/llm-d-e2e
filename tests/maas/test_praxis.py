"""Unit tests for the Praxis 3.6 fixtures (tests/maas/praxis.py).

All cluster interaction goes through ``FakeKubectl``; no cluster is required.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from conformance.deployer import Deployer

from maas.praxis import (
    PraxisTenant,
    check_praxis_prerequisites,
    create_praxis_tenant,
    praxis_profile_enabled,
    praxis_tenant_name,
    wait_aitenant_ready,
)

RUN_ID = "deadbeef"
NAME = "e2e-praxis-deadbeef"
TENANT_NS = f"ai-tenant-{NAME}"


def _conditions(*pairs: tuple[str, str]) -> list[dict]:
    return [{"type": kind, "status": status} for kind, status in pairs]


class FakeKubectl:
    """Records calls; serves canned ``get`` JSON, applies temp-file manifests, tracks deletes."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.applied: list[dict] = []
        self.deleted: list[tuple] = []
        self.patches: list[tuple] = []
        self.responses: dict[tuple[str, str], object] = {}
        self.deleted_keys: set[tuple[str, str]] = set()

    def serve(self, kind: str, name: str, *payloads: object) -> None:
        """Serve ``get kind name``: pops payloads in order, the last one repeats.

        A single callable payload is invoked on every call (dynamic state).
        """
        if len(payloads) == 1 and callable(payloads[0]):
            self.responses[(kind, name)] = payloads[0]
        else:
            self.responses[(kind, name)] = list(payloads)

    def __call__(self, *args: str, check: bool = True) -> str:
        self.calls.append(args)
        verb = args[0]
        if verb == "apply":
            path = args[args.index("-f") + 1]
            with open(path) as handle:
                self.applied.append(yaml.safe_load(handle))
            return "applied"
        if verb == "delete":
            self.deleted.append(args)
            self.deleted_keys.add((args[1], args[2]))
            return ""
        if verb == "patch":
            self.patches.append(args)
            return ""
        if verb == "get":
            kind, name = args[1], args[2]
            if (kind, name) in self.deleted_keys:
                raise RuntimeError(f"kubectl get {kind} {name} failed: NotFound")
            payload = self.responses.get((kind, name))
            if payload is None:
                raise RuntimeError(f"kubectl get {kind} {name} failed: NotFound")
            if callable(payload):
                return payload()
            if isinstance(payload, list):
                return payload.pop(0) if len(payload) > 1 else payload[0]
            return payload
        raise AssertionError(f"unexpected kubectl call: {args}")


def _namespace_that_disappears_with(fake: FakeKubectl, trigger: tuple[str, str]) -> object:
    """Namespace JSON that vanishes once the trigger resource is deleted (controller cleanup)."""

    def state() -> str:
        if trigger in fake.deleted_keys:
            raise RuntimeError("kubectl get namespace failed: NotFound")
        return json.dumps({"metadata": {"name": "tenant"}})

    return state


def _serve_cluster(
    fake: FakeKubectl,
    *,
    available: bool = True,
    extproc_entry: list | None = None,
    extproc_configmap: bool = True,
) -> None:
    """Serve the read-only 3.6 stack prerequisites."""
    fake.serve("namespace", "ai-tenants", json.dumps({"metadata": {"name": "ai-tenants"}}))
    fake.serve("namespace", "redhat-ods-applications", json.dumps({"metadata": {"name": "redhat-ods-applications"}}))
    fake.serve("crd", "aitenants.maas.opendatahub.io", json.dumps({"metadata": {"name": "aitenants"}}))
    fake.serve("crd", "maastenantconfigs.maas.opendatahub.io", json.dumps({"metadata": {"name": "maastenantconfigs"}}))
    fake.serve("gatewayclass", "istio", json.dumps({"metadata": {"name": "istio"}}))
    if extproc_entry is None:
        extproc_entry = [
            {
                "name": "RELATED_IMAGE_ODH_PRAXIS_EXTPROC_IMAGE",
                "valueFrom": {
                    "configMapKeyRef": {"name": "ai-gateway-controller-parameters", "key": "praxis-extproc-image"}
                },
            }
        ]
    fake.serve(
        "deployment",
        "ai-gateway-controller",
        json.dumps(
            {
                "status": {"conditions": _conditions(("Available", "True" if available else "False"))},
                "spec": {"template": {"spec": {"containers": [{"name": "manager", "env": extproc_entry}]}}},
            }
        ),
    )
    if extproc_configmap:
        fake.serve(
            "configmap",
            "ai-gateway-controller-parameters",
            json.dumps({"data": {"praxis-extproc-image": "quay.io/praxis-extproc@sha256:abc"}}),
        )


def _aitenant_json(*, phase: str, tenant_namespace: str = "", ready: str = "True") -> str:
    status: dict = {"phase": phase, "conditions": _conditions(("Ready", ready))}
    if tenant_namespace:
        status["tenantNamespace"] = tenant_namespace
        status["gatewayRef"] = {"name": NAME, "namespace": "redhat-ods-applications"}
    return json.dumps({"status": status})


def _serve_bootstrap(fake: FakeKubectl) -> None:
    """Serve a bootstrap that programs the Gateway, then reports a Ready AITenant."""
    fake.serve(
        "gateway",
        NAME,
        json.dumps({"status": {"conditions": _conditions(("Programmed", "False"))}}),
        json.dumps({"status": {"conditions": _conditions(("Programmed", "True"))}}),
    )
    fake.serve(
        "aitenants",
        NAME,
        _aitenant_json(phase="Pending"),
        _aitenant_json(phase="Pending", tenant_namespace=TENANT_NS, ready="False"),
        _aitenant_json(phase="Active", tenant_namespace=TENANT_NS),
    )
    fake.serve("maastenantconfigs", "default-tenant", _aitenant_json(phase="Active"))


def _deployer(fake: FakeKubectl) -> Deployer:
    deployer = Deployer()
    deployer.kubectl = fake  # type: ignore[method-assign]
    return deployer


# --- profile gate -----------------------------------------------------------


def test_profile_gate_enables_only_the_maas_36_profile():
    assert praxis_profile_enabled("configs/profiles/maas-3.6.yaml")
    assert praxis_profile_enabled("/anywhere/maas-3.6.yaml")
    assert not praxis_profile_enabled("configs/profiles/maas-3.5.yaml")
    assert not praxis_profile_enabled("configs/profiles/3.6.yaml")
    assert not praxis_profile_enabled("configs/profiles/3.5.yaml")
    assert not praxis_profile_enabled("")


# --- generated names ---------------------------------------------------------


def test_tenant_names_are_unique_and_dns_label_safe():
    first = praxis_tenant_name("0f1e2d3c")
    second = praxis_tenant_name(RUN_ID)
    assert first == "e2e-praxis-0f1e2d3c"
    assert first != second
    for name in (first, second, praxis_tenant_name("a1b2c3d4")):
        assert len(name) <= 63
        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name), name


# --- bootstrap manifests -----------------------------------------------------


def test_bootstrap_manifests_are_test_owned_and_clusterip():
    from maas import praxis

    configmap = praxis.bootstrap_configmap_manifest(NAME, praxis.GATEWAY_NAMESPACE)
    gateway = praxis.bootstrap_gateway_manifest(NAME, praxis.GATEWAY_NAMESPACE)
    aitenant = praxis.aitenant_manifest(NAME, praxis.AITENANT_NAMESPACE, gateway_name=NAME)

    assert configmap["metadata"]["name"] == f"{NAME}-config"
    assert configmap["data"]["service"] == "spec:\n  type: ClusterIP\n"
    assert gateway["spec"]["gatewayClassName"] == praxis.GATEWAY_CLASS
    assert gateway["spec"]["infrastructure"]["parametersRef"]["name"] == f"{NAME}-config"
    assert gateway["spec"]["listeners"][0]["allowedRoutes"] == {"namespaces": {"from": "All"}}
    assert aitenant["metadata"]["namespace"] == praxis.AITENANT_NAMESPACE
    assert aitenant["spec"]["gateway"]["name"] == NAME


# --- prerequisites -----------------------------------------------------------


def test_prerequisites_pass_when_the_3_6_stack_is_installed():
    fake = FakeKubectl()
    _serve_cluster(fake)

    check_praxis_prerequisites(fake)


@pytest.mark.parametrize(
    ("kind", "name", "expected"),
    [
        ("namespace", "ai-tenants", r"namespace/ai-tenants"),
        ("namespace", "redhat-ods-applications", r"namespace/redhat-ods-applications"),
        ("crd", "aitenants.maas.opendatahub.io", r"crd/aitenants\.maas\.opendatahub\.io"),
        ("crd", "maastenantconfigs.maas.opendatahub.io", r"crd/maastenantconfigs\.maas\.opendatahub\.io"),
        ("gatewayclass", "istio", r"gatewayclass/istio"),
    ],
)
def test_prerequisites_name_the_exact_missing_resource(kind, name, expected):
    fake = FakeKubectl()
    _serve_cluster(fake)
    fake.responses.pop((kind, name))

    with pytest.raises(RuntimeError, match=expected):
        check_praxis_prerequisites(fake)


def test_prerequisites_require_an_available_controller():
    fake = FakeKubectl()
    _serve_cluster(fake, available=False)

    with pytest.raises(RuntimeError, match="ai-gateway-controller.*not Available"):
        check_praxis_prerequisites(fake)


def test_prerequisites_require_a_configured_extproc_image():
    fake = FakeKubectl()
    _serve_cluster(fake, extproc_configmap=False)

    with pytest.raises(RuntimeError, match="Praxis extproc image"):
        check_praxis_prerequisites(fake)


def test_extproc_image_resolves_literal_env_values_without_the_configmap():
    fake = FakeKubectl()
    _serve_cluster(
        fake, extproc_entry=[{"name": "RELATED_IMAGE_ODH_PRAXIS_EXTPROC_IMAGE", "value": "quay.io/x@sha256:1"}]
    )

    check_praxis_prerequisites(fake)

    assert not any(call[:2] == ("get", "configmap") for call in fake.calls)


# --- tenant lifecycle --------------------------------------------------------


def test_create_praxis_tenant_bootstraps_a_ready_tenant():
    fake = FakeKubectl()
    _serve_cluster(fake)
    _serve_bootstrap(fake)
    fake.serve("namespace", TENANT_NS, _namespace_that_disappears_with(fake, ("aitenants", NAME)))

    with create_praxis_tenant(
        _deployer(fake), timeout=5, poll_interval=0, run_id=RUN_ID, teardown_timeout=0.5
    ) as tenant:
        assert tenant == PraxisTenant(
            aitenant_name=NAME,
            aitenant_namespace="ai-tenants",
            tenant_namespace=TENANT_NS,
            gateway_name=NAME,
            gateway_namespace="redhat-ods-applications",
        )
        assert [doc["kind"] for doc in fake.applied] == ["ConfigMap", "Gateway", "AITenant"]

    deleted = [(args[1], args[2]) for args in fake.deleted]
    # the AITenant goes first; the Gateway only after the tenant namespace is gone
    assert deleted.index(("aitenants", NAME)) < deleted.index(("gateway", NAME))
    assert deleted.index(("gateway", NAME)) < deleted.index(("configmap", f"{NAME}-config"))


def test_teardown_never_touches_shared_resources():
    fake = FakeKubectl()
    _serve_cluster(fake)
    _serve_bootstrap(fake)
    fake.serve("namespace", TENANT_NS, _namespace_that_disappears_with(fake, ("aitenants", NAME)))

    with create_praxis_tenant(_deployer(fake), timeout=5, poll_interval=0, run_id=RUN_ID, teardown_timeout=0.5):
        pass

    for args in fake.deleted + fake.patches:
        joined = " ".join(args)
        assert "maas-default-gateway" not in joined
        assert "models-as-a-service" not in joined
        if args[0] == "delete" and args[1] == "namespace":
            assert args[2].startswith("ai-tenant-e2e-praxis-")


def test_teardown_forces_stuck_test_owned_finalizers():
    fake = FakeKubectl()
    _serve_cluster(fake)
    _serve_bootstrap(fake)
    # controller cleanup is wedged: the tenant namespace never disappears on its own
    fake.serve("namespace", TENANT_NS, json.dumps({"metadata": {"name": TENANT_NS}}))

    with create_praxis_tenant(_deployer(fake), timeout=5, poll_interval=0, run_id=RUN_ID, teardown_timeout=0.2):
        pass

    patched = [(args[1], args[2]) for args in fake.patches]
    assert ("maastenantconfigs", "default-tenant") in patched
    assert ("aitenants", NAME) in patched
    deleted = [(args[1], args[2]) for args in fake.deleted]
    assert ("maastenantconfigs", "default-tenant") in deleted
    assert ("namespace", TENANT_NS) in deleted
    # the bootstrap Gateway and its ConfigMap are still cleaned up afterwards
    assert ("gateway", NAME) in deleted
    assert ("configmap", f"{NAME}-config") in deleted


def test_wait_aitenant_ready_reports_the_tenant_config_error():
    fake = FakeKubectl()
    fake.serve("aitenants", NAME, _aitenant_json(phase="Pending", tenant_namespace=TENANT_NS, ready="False"))
    fake.serve(
        "maastenantconfigs",
        "default-tenant",
        json.dumps(
            {
                "status": {
                    "phase": "Failed",
                    "conditions": [
                        {"type": "Ready", "status": "False", "message": "apply ClusterRole /maas-api is forbidden"}
                    ],
                }
            }
        ),
    )

    with pytest.raises(RuntimeError, match="apply ClusterRole /maas-api is forbidden"):
        wait_aitenant_ready(fake, NAME, timeout=30, poll_interval=0)


def test_wait_aitenant_ready_tolerates_a_missing_tenant_config():
    fake = FakeKubectl()
    fake.serve("aitenants", NAME, _aitenant_json(phase="Pending", tenant_namespace=TENANT_NS, ready="False"))

    with pytest.raises((RuntimeError, TimeoutError), match="MaasTenantConfig/default-tenant"):
        wait_aitenant_ready(fake, NAME, timeout=0.2, poll_interval=0)
