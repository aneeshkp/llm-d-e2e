"""Pytest fixtures and CLI flags for llm-d conformance tests."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from conformance.config import (
    TestCase,
    filter_by_names,
    load_profile,
    load_testcases_from_dir,
    resolve_profile,
)
from conformance.client import LLMClient
from conformance.deployer import Deployer
from conformance.metrics import Scraper
from conformance.report import Report

log = logging.getLogger("conformance")


def pytest_addoption(parser):
    parser.addoption("--profile", default="", help="Profile YAML path")
    parser.addoption("--testcase", default="", help="Test case name(s), comma-separated")
    parser.addoption("--testcase-dir", default="configs/testcases", help="Test case directory")
    parser.addoption("--platform", default="any", help="Platform: any, ocp, aks, gks")
    parser.addoption("--namespace", default="llm-conformance-test", help="Kubernetes namespace")
    parser.addoption("--kubeconfig", default="", help="Path to kubeconfig")
    parser.addoption("--report-dir", default="reports", help="Report output directory")
    parser.addoption("--mode", default="deploy", help="Mode: deploy, discover, cache")
    parser.addoption("--model-source", default="hf", help="Model source: hf, pvc")
    parser.addoption("--model", default="", help="Override model name")
    parser.addoption("--endpoint", default="", help="Service URL for discover mode")
    parser.addoption("--mock", default="", help="Simulator image (no GPU needed)")
    parser.addoption("--render-image", default="", help="vLLM CPU image for tokenizer render sidecar")
    parser.addoption("--pull-secret", default="", help="Pull secret name")
    parser.addoption("--bearer-token", default="", help="Bearer token for auth")
    parser.addoption("--disable-auth", action="store_true", help="Disable WASM auth")
    parser.addoption("--nocleanup", action="store_true", help="Keep resources after test")
    parser.addoption("--storage-class", default="", help="StorageClass for PVC")
    parser.addoption("--storage-size", default="", help="Override PVC size")
    parser.addoption("--guidellm-image", default="", help="GuideLLM benchmark image override")
    parser.addoption("--decode-node-selector", default="", help="Node selector for decode pods (key=value)")
    parser.addoption("--prefill-node-selector", default="", help="Node selector for prefill pods (key=value)")


def _resolve_test_cases(config) -> list[TestCase]:
    testcase_dir = config.getoption("--testcase-dir")
    profile_path = config.getoption("--profile")
    testcase_names = config.getoption("--testcase")

    if profile_path:
        profile = load_profile(profile_path)
        return resolve_profile(profile, testcase_dir)

    all_cases = load_testcases_from_dir(testcase_dir)
    if testcase_names:
        names = [n.strip() for n in testcase_names.split(",")]
        return filter_by_names(all_cases, names)

    return all_cases


def pytest_generate_tests(metafunc):
    if "tc" in metafunc.fixturenames:
        cases = _resolve_test_cases(metafunc.config)
        metafunc.parametrize("tc", cases, ids=[tc.name for tc in cases], scope="class")


@pytest.fixture(scope="session")
def deployer(request) -> Deployer:
    d = Deployer(
        kubeconfig=request.config.getoption("--kubeconfig"),
        platform=request.config.getoption("--platform"),
        namespace=request.config.getoption("--namespace"),
        model_source=request.config.getoption("--model-source"),
        mock_image=request.config.getoption("--mock"),
        render_image=request.config.getoption("--render-image"),
        pull_secret=request.config.getoption("--pull-secret"),
        disable_auth=request.config.getoption("--disable-auth"),
        decode_node_selector=request.config.getoption("--decode-node-selector"),
        prefill_node_selector=request.config.getoption("--prefill-node-selector"),
    )
    yield d
    d.stop_port_forward()


@pytest.fixture(scope="session")
def report(request) -> Report:
    r = Report(
        profile=request.config.getoption("--profile"),
        platform=request.config.getoption("--platform"),
    )
    yield r
    r.finalize(request.config.getoption("--report-dir"))


@pytest.fixture(scope="class")
def endpoint(deployer: Deployer, tc: TestCase, request) -> str:
    explicit = request.config.getoption("--endpoint")
    if explicit:
        return explicit
    return deployer.get_endpoint(tc.name)


@pytest.fixture(scope="class")
def client(endpoint: str, request) -> LLMClient:
    c = LLMClient(base_url=endpoint, bearer_token=request.config.getoption("--bearer-token"))
    yield c
    c.close()


@pytest.fixture(scope="class")
def pod_endpoint(deployer: Deployer, tc: TestCase) -> str:
    return deployer.get_pod_endpoint(tc.name)


@pytest.fixture(scope="class")
def pod_client(pod_endpoint: str, request) -> LLMClient:
    c = LLMClient(base_url=pod_endpoint, bearer_token=request.config.getoption("--bearer-token"))
    yield c
    c.close()


@pytest.fixture(scope="class")
def scraper(deployer: Deployer) -> Scraper:
    return Scraper(kubectl_fn=deployer.kubectl, namespace=deployer.namespace, kubeconfig=deployer.kubeconfig)


@pytest.fixture(scope="session")
def no_cleanup(request) -> bool:
    return request.config.getoption("--nocleanup")


@pytest.fixture(scope="session")
def test_mode(request) -> str:
    return request.config.getoption("--mode")


@pytest.fixture(scope="session")
def guidellm_image(request) -> str:
    return request.config.getoption("--guidellm-image")
