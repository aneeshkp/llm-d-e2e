"""Praxis (payload processing) 3.6-only test fixtures.

Ported from upstream ``opendatahub-tests`` ``tests/ai_gateway/models_as_a_service/praxis``,
adapted for xKS: the AITenant admission webhook enforces a dedicated Gateway per AITenant
(maas-controller ``pkg/webhook/aitenant_webhook.go``), so every run provisions its own
test-owned bootstrap Gateway. The Gateway's infrastructure ConfigMap pins the generated
Service to ``ClusterIP`` — no LoadBalancer, no public IP — mirroring upstream's
``bootstrap_gateway_context``. The shared ``maas-default-gateway``, its Service, and the
``models-as-a-service`` default tenant are never created, patched, or deleted.

Resource identifiers are pinned from upstream ``praxis/constants.py`` and maas-controller
``pkg/platform/tenantreconcile``: the payload-processing-type annotation and its ``praxis``
/``ipp`` values, the praxis-cleanup finalizer, and the per-tenant legacy-IPP resource names.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import yaml

from conformance.deployer import Deployer

# The only profile that selects the Praxis phases.
PRAXIS_PROFILE = "maas-3.6.yaml"

# Shared, chart-managed locations the fixtures consume but never modify.
AITENANT_NAMESPACE = "ai-tenants"
GATEWAY_NAMESPACE = "redhat-ods-applications"
GATEWAY_CLASS = "istio"
AITENANT_CRD = "aitenants.maas.opendatahub.io"
MAASTENANTCONFIG_CRD = "maastenantconfigs.maas.opendatahub.io"
CONTROLLER_DEPLOYMENT = "ai-gateway-controller"
PRAXIS_EXTPROC_IMAGE_ENV = "RELATED_IMAGE_ODH_PRAXIS_EXTPROC_IMAGE"

# The namespace-local MaasTenantConfig generated for every AITenant.
TENANT_CONFIG_NAME = "default-tenant"

# Payload-processing contract (upstream praxis/constants.py).
PAYLOAD_PROCESSING_TYPE_ANNOTATION = "maas.opendatahub.io/payload-processing-type"
PAYLOAD_PROCESSING_STATUS_ANNOTATION = "maas.opendatahub.io/payload-processing-status"
CLEANUP_COMPLETE_VALUE = "cleanup-complete"
PRAXIS_VALUE = "praxis"
IPP_VALUE = "ipp"
PRAXIS_CLEANUP_FINALIZER = "ai-gateway-controller.opendatahub.io/praxis-cleanup"

# A persistent not-Ready detail across this many polls means the controller is stuck.
_FAILED_POLLS = 3

# Cleanup must finish even when the controller cannot (see the forced-finalizer path).
_FORCE_WAIT_SECONDS = 60


def praxis_profile_enabled(profile_path: str) -> bool:
    """True only when the pytest ``--profile`` resolves to the 3.6 MaaS profile."""
    return bool(profile_path) and Path(profile_path).name == PRAXIS_PROFILE


def praxis_tenant_name(run_id: str) -> str:
    """Unique, DNS-label-safe AITenant name for one Praxis run."""
    return f"e2e-praxis-{run_id}"


@dataclass(frozen=True)
class PraxisTenant:
    """The test-owned AITenant and its generated resources."""

    aitenant_name: str
    aitenant_namespace: str
    tenant_namespace: str
    gateway_name: str
    gateway_namespace: str


# --- generated manifests -------------------------------------------------------


def bootstrap_configmap_manifest(gateway_name: str, namespace: str) -> dict:
    """Infrastructure ConfigMap pinning the bootstrap Gateway's Service to ClusterIP."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{gateway_name}-config", "namespace": namespace},
        "data": {"service": "spec:\n  type: ClusterIP\n"},
    }


def bootstrap_gateway_manifest(gateway_name: str, namespace: str) -> dict:
    """Test-owned Gateway the AITenant webhook and MaaS reconcile require."""
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": gateway_name, "namespace": namespace},
        "spec": {
            "gatewayClassName": GATEWAY_CLASS,
            "infrastructure": {"parametersRef": {"group": "", "kind": "ConfigMap", "name": f"{gateway_name}-config"}},
            "listeners": [
                {
                    "name": "http",
                    "port": 80,
                    "protocol": "HTTP",
                    "allowedRoutes": {"namespaces": {"from": "All"}},
                }
            ],
        },
    }


def aitenant_manifest(name: str, namespace: str, gateway_name: str) -> dict:
    """The test-owned AITenant, bound to its bootstrap Gateway."""
    return {
        "apiVersion": "maas.opendatahub.io/v1alpha1",
        "kind": "AITenant",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"gateway": {"name": gateway_name}},
    }


# --- state probes --------------------------------------------------------------


def _get_json(kubectl: Callable[..., str], kind: str, name: str, namespace: str = "") -> dict:
    args = ("get", kind, name) + (("-n", namespace) if namespace else ()) + ("-o", "json")
    return json.loads(kubectl(*args))


def _condition_true(conditions: list | None, condition_type: str) -> bool:
    return any(
        condition.get("type") == condition_type and condition.get("status") == "True" for condition in conditions or []
    )


def _false_condition_messages(conditions: list | None) -> list[str]:
    messages = []
    for condition in conditions or []:
        if condition.get("status") == "False" and condition.get("message"):
            message = str(condition["message"]).strip()
            if message and message not in messages:
                messages.append(message)
    return messages


def praxis_extproc_image(kubectl: Callable[..., str], deployment: dict) -> str:
    """The controller's Praxis extproc image, from a literal env value or its ConfigMap ref."""
    entry = {}
    for container in ((deployment.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []:
        for env in container.get("env") or []:
            if env.get("name") == PRAXIS_EXTPROC_IMAGE_ENV:
                entry = env
                break
    if entry.get("value"):
        return str(entry["value"])
    ref = (entry.get("valueFrom") or {}).get("configMapKeyRef") or {}
    if not ref.get("name"):
        return ""
    try:
        configmap = _get_json(kubectl, "configmap", ref["name"], GATEWAY_NAMESPACE)
    except RuntimeError:
        return ""
    return str((configmap.get("data") or {}).get(ref.get("key") or "", ""))


def check_praxis_prerequisites(kubectl: Callable[..., str]) -> None:
    """Read-only checks for the 3.6 stack; raises naming the exact missing prerequisite."""
    for namespace in (AITENANT_NAMESPACE, GATEWAY_NAMESPACE):
        try:
            _get_json(kubectl, "namespace", namespace)
        except RuntimeError:
            raise RuntimeError(f"missing Praxis prerequisite: namespace/{namespace} not found") from None
    for crd in (AITENANT_CRD, MAASTENANTCONFIG_CRD):
        try:
            _get_json(kubectl, "crd", crd)
        except RuntimeError:
            raise RuntimeError(f"missing Praxis prerequisite: crd/{crd} not found") from None
    try:
        _get_json(kubectl, "gatewayclass", GATEWAY_CLASS)
    except RuntimeError:
        raise RuntimeError(f"missing Praxis prerequisite: gatewayclass/{GATEWAY_CLASS} not found") from None

    deployment = _get_json(kubectl, "deployment", CONTROLLER_DEPLOYMENT, GATEWAY_NAMESPACE)
    if not _condition_true((deployment.get("status") or {}).get("conditions"), "Available"):
        raise RuntimeError(
            f"missing Praxis prerequisite: deployment/{CONTROLLER_DEPLOYMENT} in {GATEWAY_NAMESPACE} is not Available"
        )
    if not praxis_extproc_image(kubectl, deployment):
        raise RuntimeError(
            f"missing Praxis prerequisite: deployment/{CONTROLLER_DEPLOYMENT} has no Praxis extproc image "
            f"configured ({PRAXIS_EXTPROC_IMAGE_ENV})"
        )


def _aitenant_not_ready_detail(kubectl: Callable[..., str], aitenant: dict) -> str:
    """Why the AITenant is not Ready: its own status plus its MaasTenantConfig's conditions."""
    status = aitenant.get("status") or {}
    parts = [f"phase={status.get('phase', 'Pending')}"]
    parts += _false_condition_messages(status.get("conditions"))
    tenant_namespace = status.get("tenantNamespace", "")
    if tenant_namespace:
        try:
            config = _get_json(kubectl, "maastenantconfigs", TENANT_CONFIG_NAME, tenant_namespace)
        except RuntimeError:
            parts.append(f"MaasTenantConfig/{TENANT_CONFIG_NAME} not found in {tenant_namespace}")
        else:
            parts += _false_condition_messages((config.get("status") or {}).get("conditions"))
    return "; ".join(parts)


def wait_aitenant_ready(kubectl: Callable[..., str], name: str, timeout: float, poll_interval: float = 5) -> dict:
    """Wait for ``AITenant/{name}`` Ready=True; return its status.

    Fails fast when the not-Ready detail is identical across consecutive polls (a stuck
    controller, e.g. the tenant-config reconcile error) and otherwise times out with the
    observed status, always including the MaasTenantConfig conditions as diagnostics.
    """
    deadline = time.monotonic() + timeout
    persistent_detail = ""
    strikes = 0
    while True:
        aitenant = _get_json(kubectl, "aitenants", name, AITENANT_NAMESPACE)
        status = aitenant.get("status") or {}
        if _condition_true(status.get("conditions"), "Ready"):
            return status
        detail = _aitenant_not_ready_detail(kubectl, aitenant)
        strikes = strikes + 1 if detail == persistent_detail else 1
        persistent_detail = detail
        if strikes >= _FAILED_POLLS:
            raise RuntimeError(f"AITenant/{name} is not Ready: {detail}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out after {timeout:g}s waiting for AITenant/{name} Ready: {detail}")
        time.sleep(poll_interval)


# --- tenant lifecycle ----------------------------------------------------------


def _apply(kubectl: Callable[..., str], manifest: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(manifest, handle)
        path = handle.name
    try:
        kubectl("apply", "-f", path)
    finally:
        os.unlink(path)


def _wait_gateway_programmed(
    kubectl: Callable[..., str], name: str, namespace: str, timeout: float, poll_interval: float
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        gateway = _get_json(kubectl, "gateway", name, namespace)
        conditions = (gateway.get("status") or {}).get("conditions")
        if _condition_true(conditions, "Programmed"):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout:g}s waiting for Gateway/{name} Programmed (conditions={json.dumps(conditions)})"
            )
        time.sleep(poll_interval)


def _wait_aitenant_status(
    kubectl: Callable[..., str], name: str, timeout: float, poll_interval: float
) -> tuple[str, str, str]:
    """Wait for the generated ``tenantNamespace`` and ``gatewayRef``; return both."""
    deadline = time.monotonic() + timeout
    while True:
        status = _get_json(kubectl, "aitenants", name, AITENANT_NAMESPACE).get("status") or {}
        gateway_ref = status.get("gatewayRef") or {}
        if status.get("tenantNamespace") and gateway_ref.get("name") and gateway_ref.get("namespace"):
            return status["tenantNamespace"], gateway_ref["name"], gateway_ref["namespace"]
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout:g}s waiting for AITenant/{name} status.tenantNamespace/gatewayRef "
                f"(status={json.dumps(status)})"
            )
        time.sleep(poll_interval)


def _namespace_exists(kubectl: Callable[..., str], namespace: str) -> bool:
    try:
        kubectl("get", "namespace", namespace, "-o", "json")
        return True
    except RuntimeError:
        return False


def _wait_namespace_gone(kubectl: Callable[..., str], namespace: str, timeout: float, poll_interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _namespace_exists(kubectl, namespace):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _clear_finalizers(kubectl: Callable[..., str], kind: str, name: str, namespace: str) -> None:
    try:
        kubectl("patch", kind, name, "-n", namespace, "--type=merge", "-p", '{"metadata":{"finalizers":[]}}')
    except RuntimeError as error:
        print(f"  → could not clear finalizers on {kind}/{name}: {error}")


def _force_tenant_cleanup(kubectl: Callable[..., str], tenant: PraxisTenant) -> None:
    """Break-glass cleanup of test-owned tenant resources when controller cleanup is stuck.

    The generated MaasTenantConfig and AITenant keep their cleanup finalizers for as long
    as the controller cannot complete the platform reconcile; clearing them (and deleting
    the generated namespace) is limited to resources this run created.
    """
    print(f"  → AITenant cleanup is stuck; forcing test-owned cleanup for {tenant.tenant_namespace}")
    _clear_finalizers(kubectl, "maastenantconfigs", TENANT_CONFIG_NAME, tenant.tenant_namespace)
    kubectl("delete", "maastenantconfigs", TENANT_CONFIG_NAME, "-n", tenant.tenant_namespace, "--ignore-not-found")
    _clear_finalizers(kubectl, "aitenants", tenant.aitenant_name, tenant.aitenant_namespace)
    kubectl("delete", "namespace", tenant.tenant_namespace, "--ignore-not-found")


def _teardown(kubectl: Callable[..., str], tenant: PraxisTenant, timeout: float, poll_interval: float) -> None:
    """Delete only test-owned resources, in the order the controller requires.

    The MaasTenantConfig cleanup path needs the Gateway to exist, so the bootstrap Gateway
    and its ConfigMap are deleted only after the tenant namespace is gone.
    """
    kubectl("delete", "aitenants", tenant.aitenant_name, "-n", tenant.aitenant_namespace, "--ignore-not-found")
    if not _wait_namespace_gone(kubectl, tenant.tenant_namespace, timeout, poll_interval):
        _force_tenant_cleanup(kubectl, tenant)
        if not _wait_namespace_gone(kubectl, tenant.tenant_namespace, _FORCE_WAIT_SECONDS, poll_interval):
            raise RuntimeError(f"tenant namespace {tenant.tenant_namespace} still exists after forced cleanup")
    kubectl("delete", "gateway", tenant.gateway_name, "-n", tenant.gateway_namespace, "--ignore-not-found")
    kubectl(
        "delete", "configmap", f"{tenant.gateway_name}-config", "-n", tenant.gateway_namespace, "--ignore-not-found"
    )


@contextmanager
def create_praxis_tenant(
    deployer: Deployer,
    timeout: float = 600,
    poll_interval: float = 5,
    run_id: str | None = None,
    teardown_timeout: float = 180,
) -> Iterator[PraxisTenant]:
    """Yield a Ready, test-owned AITenant with its bootstrap Gateway; clean everything up.

    The AITenant admission webhook requires a dedicated Gateway per tenant, so each run
    provisions a ClusterIP bootstrap Gateway in the shared gateway namespace. Only
    test-owned resources are created or deleted; teardown runs even when the body fails.
    """
    kubectl = deployer.kubectl
    check_praxis_prerequisites(kubectl)
    name = praxis_tenant_name(run_id or uuid.uuid4().hex[:8])
    _apply(kubectl, bootstrap_configmap_manifest(name, GATEWAY_NAMESPACE))
    _apply(kubectl, bootstrap_gateway_manifest(name, GATEWAY_NAMESPACE))
    _wait_gateway_programmed(kubectl, name, GATEWAY_NAMESPACE, timeout, poll_interval)
    _apply(kubectl, aitenant_manifest(name, AITENANT_NAMESPACE, gateway_name=name))
    tenant_namespace, gateway_name, gateway_namespace = _wait_aitenant_status(kubectl, name, timeout, poll_interval)
    wait_aitenant_ready(kubectl, name, timeout, poll_interval)
    tenant = PraxisTenant(
        aitenant_name=name,
        aitenant_namespace=AITENANT_NAMESPACE,
        tenant_namespace=tenant_namespace,
        gateway_name=gateway_name,
        gateway_namespace=gateway_namespace,
    )
    try:
        yield tenant
    finally:
        _teardown(kubectl, tenant, teardown_timeout, poll_interval)
