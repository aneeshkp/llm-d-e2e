"""MaaS conformance phases.

``MaaSPhases`` is mixed into ``TestConformance`` (tests/test_conformance.py) so
these phases run for each testcase after the llm-d phases have validated the
model and before cleanup. They skip unless the testcase manifest declares a
``MaaSModelRef``; the manifest's MaaS resources are applied with the model in
phase 02, so a MaaS testcase is just a testcase config plus its manifest.

To add a MaaS check, add a ``test_30<letter>_maas_<name>`` method here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.config import TestCase
from conformance.deployer import Deployer
from conformance.maas import (
    MaaSClient,
    MaaSTarget,
    create_user_token,
    endpoint_with_scheme,
    manifest_maas_resources,
    wait_for_maas_ready,
)

# How many requests fit in the subscription's token quota depends on response lengths,
# so send short requests until the quota is spent; this bound fails a never-enforced limit.
RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_MAX_TOKENS = 50


_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "deploy" / "manifests"


def _declares_model_ref(tc: TestCase) -> bool:
    resources = manifest_maas_resources(_MANIFEST_DIR / tc.deployment.manifest_path, "")
    return any(resource.kind == "MaaSModelRef" for resource in resources)


# Applied per method: a class-level mark would be inherited by every TestConformance phase.
# with_args: a mark called with a lone callable would treat it as the decorated function.
_for_maas_manifests = pytest.mark.applies_when.with_args(_declares_model_ref)


class MaaSPhases:
    """Governed MaaS route checks for testcases whose manifest declares MaaS resources."""

    @pytest.fixture(scope="class")
    def maas_target(self, deployer: Deployer, tc: TestCase, test_mode: str) -> MaaSTarget:
        if test_mode != "discover" and not deployer.is_deployed(tc.name):
            pytest.skip(f"skipped — deploy failed or was skipped for '{tc.name}'")
        resources = manifest_maas_resources(deployer.manifest_dir / tc.deployment.manifest_path, deployer.namespace)
        return MaaSTarget(resources=resources)

    @pytest.fixture(scope="class")
    def maas_client(self, maas_target: MaaSTarget, tc: TestCase) -> MaaSClient:
        if not maas_target.endpoint:
            pytest.skip("MaaS route is not ready — see test_30a_maas_ready")
        client = MaaSClient(maas_target.endpoint, timeout=tc.validation.timeout.total_seconds())
        yield client
        client.close()

    @_for_maas_manifests
    def test_30a_maas_ready(self, deployer: Deployer, tc: TestCase, maas_target: MaaSTarget):
        """MaaSModelRef Ready, auth policy and subscription Active; the route has an endpoint and alias."""
        status = wait_for_maas_ready(
            deployer.kubectl, maas_target.resources, timeout=tc.deployment.ready_timeout.total_seconds()
        )
        assert status.get("endpoint"), "MaaSModelRef status has no endpoint"
        assert status.get("resolvedModelAlias"), "MaaSModelRef status has no resolvedModelAlias"
        maas_target.endpoint = endpoint_with_scheme(status["endpoint"], tc.validation.maas.endpoint_scheme)
        maas_target.model_alias = status["resolvedModelAlias"]
        print(f"  → MaaS endpoint {maas_target.endpoint}, model {maas_target.model_alias}")

    @_for_maas_manifests
    def test_30b_maas_unauthenticated(self, maas_client: MaaSClient, maas_target: MaaSTarget):
        """Inference without an API key is rejected."""
        response = maas_client.chat_completion(maas_target.model_alias, "hello")
        assert response.status_code == 401, response.text

    @_for_maas_manifests
    def test_30c_maas_api_key(self, deployer: Deployer, maas_client: MaaSClient, maas_target: MaaSTarget):
        """A Kubernetes user token can create an API key."""
        response = maas_client.create_api_key(create_user_token(deployer.kubectl), "llm-d-e2e")
        assert response.status_code == 201, response.text
        maas_target.api_key = response.json().get("key", "")
        assert maas_target.api_key.startswith("sk-oai-"), "API-key creation must return the plaintext key once"

    @_for_maas_manifests
    def test_30d_maas_inference(self, maas_client: MaaSClient, maas_target: MaaSTarget):
        """Authenticated inference through the MaaS route succeeds."""
        if not maas_target.api_key:
            pytest.skip("no API key — see test_30c_maas_api_key")
        response = maas_client.chat_completion(maas_target.model_alias, "hello", api_key=maas_target.api_key)
        assert response.status_code == 200, response.text
        assert response.json().get("choices"), "authenticated MaaS inference must return a completion"

    @_for_maas_manifests
    def test_30e_maas_rate_limit(self, maas_client: MaaSClient, maas_target: MaaSTarget):
        """The subscription's token rate limit rejects requests once the quota is spent."""
        if not maas_target.api_key:
            pytest.skip("no API key — see test_30c_maas_api_key")
        statuses: list[int] = []
        for _ in range(RATE_LIMIT_MAX_REQUESTS):
            response = maas_client.chat_completion(
                maas_target.model_alias,
                "Write a long essay about AI",
                api_key=maas_target.api_key,
                max_tokens=RATE_LIMIT_MAX_TOKENS,
            )
            statuses.append(response.status_code)
            if response.status_code != 200:
                break
        print(f"  → {statuses.count(200)} request(s) within quota, then {statuses[-1]}")
        assert statuses[0] == 200, f"the first request within quota should succeed; got {statuses}"
        assert statuses[-1] == 429, f"requests over quota must get 429; got {statuses}"
