"""Smoke tests for framework validation — no cluster required."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from conformance.config import load_testcase, load_profile, load_testcases_from_dir, parse_duration
from conformance.metrics import parse_prometheus
from conformance.client import LLMClient


def test_parse_duration():
    assert parse_duration("15m").total_seconds() == 900
    assert parse_duration("2h").total_seconds() == 7200
    assert parse_duration("300s").total_seconds() == 300
    assert parse_duration("1h30m").total_seconds() == 5400


def test_load_testcase():
    tc = load_testcase("configs/testcases/single-gpu-smoke.yaml")
    assert tc.name == "single-gpu-smoke"
    assert tc.model.name == "Qwen/Qwen3-0.6B"
    assert tc.deployment.replicas == 1
    assert tc.deployment.resources.gpus == 1
    assert tc.validation.health_port == 8000
    assert tc.validation.test_prompts


def test_load_profile():
    profile = load_profile("configs/profiles/smoke.yaml")
    assert profile.name == "smoke"
    assert "single-gpu-smoke" in profile.test_cases


def test_load_all_testcases():
    cases = load_testcases_from_dir("configs/testcases")
    assert len(cases) >= 1
    names = [tc.name for tc in cases]
    assert "single-gpu-smoke" in names


def test_parse_prometheus_text():
    text = """# HELP vllm:request_success_total Total requests
# TYPE vllm:request_success_total counter
vllm:request_success_total{model_name="Qwen/Qwen3-0.6B"} 42.0
vllm:gpu_cache_usage_perc 0.15
"""
    metrics = parse_prometheus(text)
    assert "vllm:request_success_total" in metrics
    assert metrics["vllm:request_success_total"][0].value == 42.0
    assert metrics["vllm:request_success_total"][0].labels["model_name"] == "Qwen/Qwen3-0.6B"
    assert metrics["vllm:gpu_cache_usage_perc"][0].value == 0.15


def test_llm_client_init():
    c = LLMClient(base_url="http://localhost:8000", bearer_token="test-token")
    assert c._client.headers.get("authorization") == "Bearer test-token"
    c.close()
