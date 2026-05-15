"""LLM-D conformance test suite.

Each test case runs through ordered phases:
  01. Prerequisites — CRD exists
  02. Deploy — apply LLMInferenceService manifest
  03. Service — wait for Service creation
  04. Gateway — wait for Gateway to be programmed
  05. Pods — wait for pods to be Running
  06. Ready — wait for LLMInferenceService Ready=True
  07. Health — GET /health returns 200
  08. Models — GET /v1/models lists the model
  09. Inference — POST /v1/chat/completions returns tokens
  10. Metrics — scrape and validate Prometheus metrics
  99. Cleanup — delete LLMInferenceService
"""

from __future__ import annotations

import time

import pytest

from conformance.config import TestCase
from conformance.client import LLMClient
from conformance.deployer import Deployer
from conformance.metrics import (
    Scraper,
    validate_cache_aware,
    validate_pd,
    validate_scheduler,
    validate_vllm_basic,
)
from conformance.report import Report, TestResult

LLMISVC_CRD = "llminferenceservices.serving.kserve.io"


def _log(msg: str, capsys=None):
    print(f"  → {msg}")


class TestConformance:
    """Ordered conformance phases for each test case."""

    def test_01_prereq(self, deployer: Deployer, tc: TestCase):
        """LLMInferenceService CRD must be installed."""
        found = deployer.check_crd_exists(LLMISVC_CRD)
        _log(f"CRD {LLMISVC_CRD}: {'found' if found else 'NOT FOUND'}")
        assert found, f"CRD {LLMISVC_CRD} not found"

    def test_02_deploy(self, deployer: Deployer, tc: TestCase, test_mode: str):
        """Deploy the LLMInferenceService manifest."""
        if test_mode == "discover":
            pytest.skip("discover mode — skipping deploy")
        _log(f"Deploying {tc.deployment.manifest_path} as '{tc.name}'")
        result = deployer.deploy(tc)
        _log(f"Deploy {'succeeded' if result.success else 'FAILED'} in {result.duration:.1f}s")
        if result.error:
            _log(f"Error: {result.error}")
        assert result.success, f"Deploy failed: {result.error}"

    def test_03_service(self, deployer: Deployer, tc: TestCase):
        """Service should be created for the LLMInferenceService."""
        _log(f"Waiting for Service for '{tc.name}'...")
        svc = deployer.wait_for_service(tc.name)
        _log(f"Service found: {svc}")

    def test_04_gateway(self, deployer: Deployer, tc: TestCase):
        """Gateway should be programmed with an address."""
        _log("Waiting for Gateway to be programmed...")
        addr = deployer.wait_for_gateway()
        _log(f"Gateway address: {addr}")
        assert addr, "Gateway has no address"

    def test_05_pods(self, deployer: Deployer, tc: TestCase):
        """Pods should be Running without crashes."""
        timeout = tc.deployment.ready_timeout.total_seconds()
        _log(f"Waiting for pods to be Running (timeout: {timeout:.0f}s)")
        pods = deployer.wait_for_pods(tc.name, timeout=timeout, print_fn=_log)
        _log(f"All pods running: {', '.join(pods)}")
        assert len(pods) >= tc.deployment.replicas, (
            f"Expected {tc.deployment.replicas} pods, got {len(pods)}"
        )

    def test_06_ready(self, deployer: Deployer, tc: TestCase):
        """LLMInferenceService should become Ready."""
        _log(f"Waiting for '{tc.name}' Ready=True")
        deployer.wait_for_ready(tc, print_fn=_log)
        _log(f"'{tc.name}' is Ready")

    def test_07_health(self, client: LLMClient, tc: TestCase, endpoint: str):
        """Health endpoint should return 200."""
        max_retries = max(tc.validation.retry_attempts, 10)
        interval = tc.validation.retry_interval.total_seconds() or 15
        _log(f"Endpoint: {endpoint}")
        _log(f"Checking health (up to {max_retries} attempts, {interval:.0f}s interval)")
        for attempt in range(max_retries):
            try:
                client.health_check()
                _log(f"Health check PASSED (attempt {attempt + 1})")
                return
            except Exception as e:
                err_short = str(e).split("\n")[0][:120]
                _log(f"Attempt {attempt + 1}/{max_retries}: {err_short}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(interval)

    def test_08_models(self, client: LLMClient, tc: TestCase):
        """Model should be listed in /v1/models."""
        resp = client.list_models()
        models = [m["id"] for m in resp.get("data", [])]
        _log(f"Models listed: {models}")
        assert models, "No models returned"

    def test_09_inference(self, client: LLMClient, tc: TestCase):
        """Inference should return tokens for test prompts."""
        if not tc.validation.inference_check:
            pytest.skip("inference check disabled")
        prompts = tc.validation.test_prompts or ["What is 2+2?"]
        for prompt in prompts:
            _log(f"Sending prompt: '{prompt[:50]}...'")
            resp = client.chat(model=tc.model.name, prompt=prompt)
            choices = resp.get("choices", [])
            assert choices, f"No choices returned for prompt: {prompt}"
            content = choices[0].get("message", {}).get("content", "")
            tokens = resp.get("usage", {}).get("total_tokens", 0)
            _log(f"Response: '{content[:80]}...' ({tokens} tokens)")
            assert content, f"Empty response for prompt: {prompt}"
            assert tokens > 0, "No tokens generated"

    def test_10_metrics_vllm(self, scraper: Scraper, tc: TestCase):
        """vLLM metrics should show successful requests."""
        mc = tc.validation.metrics_check
        if not mc.enabled or not mc.check_vllm:
            pytest.skip("vLLM metrics check disabled")
        _log("Scraping vLLM metrics...")
        results = scraper.scrape_vllm(tc.name)
        _log(f"Scraped {len(results)} pod(s)")
        assert results, "No vLLM metrics scraped"
        checks = validate_vllm_basic(results)
        for c in checks:
            _log(f"  {c.name}: {'PASS' if c.passed else 'FAIL'} — {c.message}")
        failed = [c for c in checks if not c.passed]
        assert not failed, f"vLLM metric checks failed: {[c.message for c in failed]}"

    def test_11_metrics_cache(self, scraper: Scraper, tc: TestCase):
        """Prefix cache metrics should show hits."""
        mc = tc.validation.metrics_check
        if not mc.enabled or not mc.check_prefix_cache:
            pytest.skip("prefix cache check disabled")
        _log("Scraping cache-aware metrics...")
        vllm = scraper.scrape_vllm(tc.name)
        epp = scraper.scrape_epp(tc.name)
        checks = validate_cache_aware(vllm, epp)
        for c in checks:
            _log(f"  {c.name}: {'PASS' if c.passed else 'FAIL'} — {c.message}")
        failed = [c for c in checks if not c.passed]
        assert not failed, f"Cache metric checks failed: {[c.message for c in failed]}"

    def test_12_metrics_pd(self, scraper: Scraper, tc: TestCase):
        """P/D metrics should show token distribution."""
        mc = tc.validation.metrics_check
        if not mc.enabled or not mc.check_pd:
            pytest.skip("P/D metrics check disabled")
        _log("Scraping P/D metrics...")
        vllm = scraper.scrape_vllm(tc.name)
        prefill = scraper.scrape_prefill(tc.name)
        checks = validate_pd(vllm + prefill)
        for c in checks:
            _log(f"  {c.name}: {'PASS' if c.passed else 'FAIL'} — {c.message}")
        failed = [c for c in checks if not c.passed]
        assert not failed, f"P/D metric checks failed: {[c.message for c in failed]}"

    def test_13_metrics_scheduler(self, scraper: Scraper, tc: TestCase):
        """Scheduler/EPP metrics should show processed requests."""
        mc = tc.validation.metrics_check
        if not mc.enabled or not mc.check_scheduler:
            pytest.skip("scheduler metrics check disabled")
        _log("Scraping scheduler/EPP metrics...")
        epp = scraper.scrape_epp(tc.name)
        _log(f"Scraped {len(epp)} EPP pod(s)")
        assert epp, "No EPP metrics scraped"
        checks = validate_scheduler(epp)
        for c in checks:
            _log(f"  {c.name}: {'PASS' if c.passed else 'FAIL'} — {c.message}")
        failed = [c for c in checks if not c.passed]
        assert not failed, f"Scheduler metric checks failed: {[c.message for c in failed]}"

    def test_99_cleanup(self, deployer: Deployer, tc: TestCase, no_cleanup: bool):
        """Clean up deployed resources."""
        if no_cleanup:
            pytest.skip("--nocleanup set")
        if not tc.cleanup:
            pytest.skip("cleanup disabled in test case config")
        _log(f"Cleaning up '{tc.name}'...")
        deployer.cleanup(tc)
        _log("Cleanup complete")
