"""Smoke tests for framework validation — no cluster required."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def test_deployer_is_deployed():
    from conformance.deployer import Deployer

    d = Deployer()
    assert not d.is_deployed("foo")
    d._deployed.add("foo")
    assert d.is_deployed("foo")
    d._deployed.discard("foo")
    assert not d.is_deployed("foo")


def test_setup_manifests_removes_stale_files(tmp_path, monkeypatch):
    """Switching manifest branches must remove stale files from the previous branch.

    Regression: _setup_manifests used to copy new files on top of existing ones
    without pruning. Switching main→3.4-stable left flow-control-tokens.yaml
    behind, causing it to appear available when the 3.4 EPP would crash on it.
    """
    import shutil
    from unittest.mock import MagicMock, patch
    import conformance.cli as cli_mod

    monkeypatch.chdir(tmp_path)

    # Simulate manifests left over from a previous `--setup main` run
    manifest_dir = tmp_path / "deploy" / "manifests"
    manifest_dir.mkdir(parents=True)
    for stale in ["flow-control-tokens.yaml", "flow-control.yaml", "pd-performance.yaml"]:
        (manifest_dir / stale).write_text("stale: true")

    # Pre-create what `git clone` would produce for 3.4-stable
    clone_dir = Path("/tmp/llm-d-manifests")
    clone_dir.mkdir(exist_ok=True)
    for new in ["single-gpu.yaml", "cache-aware.yaml"]:
        (clone_dir / new).write_text("branch: 3.4-stable")

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "abc1234deadbeef\n"
        result.stderr = ""
        if cmd[0] == "rm":
            shutil.rmtree(str(clone_dir), ignore_errors=True)
        return result

    with patch.object(cli_mod, "subprocess") as mock_sub:
        mock_sub.run.side_effect = fake_run
        cli_mod._setup_manifests("3.4-stable")

    remaining = {f.name for f in manifest_dir.glob("*.yaml")}
    assert "flow-control-tokens.yaml" not in remaining
    assert "flow-control.yaml" not in remaining
    assert "pd-performance.yaml" not in remaining
    assert "single-gpu.yaml" in remaining
    assert "cache-aware.yaml" in remaining


def test_require_manifest_skips_when_missing(tmp_path):
    """test_01_prereq and test_02_deploy skip when the manifest file is absent."""
    from dataclasses import dataclass

    @dataclass
    class FakeDeployConfig:
        manifest_path: str = "nonexistent.yaml"

    @dataclass
    class FakeTestCase:
        deployment: FakeDeployConfig = None

        def __post_init__(self):
            self.deployment = FakeDeployConfig()

    sys.path.insert(0, str(Path(__file__).parent))
    import test_conformance as tc_mod

    original = tc_mod._MANIFEST_DIR
    try:
        tc_mod._MANIFEST_DIR = tmp_path
        with pytest.raises(pytest.skip.Exception, match="nonexistent.yaml"):
            tc_mod._require_manifest(FakeTestCase())
    finally:
        tc_mod._MANIFEST_DIR = original


def test_require_manifest_does_not_skip_when_present(tmp_path):
    """_require_manifest should not skip when the manifest exists."""
    from dataclasses import dataclass

    @dataclass
    class FakeDeployConfig:
        manifest_path: str = "exists.yaml"

    @dataclass
    class FakeTestCase:
        deployment: FakeDeployConfig = None

        def __post_init__(self):
            self.deployment = FakeDeployConfig()

    (tmp_path / "exists.yaml").write_text("kind: LLMInferenceService")

    sys.path.insert(0, str(Path(__file__).parent))
    import test_conformance as tc_mod

    original = tc_mod._MANIFEST_DIR
    try:
        tc_mod._MANIFEST_DIR = tmp_path
        tc_mod._require_manifest(FakeTestCase())
    finally:
        tc_mod._MANIFEST_DIR = original


def test_new_testcase_script_generates_loadable_config(tmp_path, monkeypatch):
    """new-testcase.sh must produce a config YAML that load_testcase() can parse."""
    import subprocess

    import yaml

    script = Path(__file__).parent.parent / "scripts" / "new-testcase.sh"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs" / "testcases").mkdir(parents=True)
    (tmp_path / "deploy" / "manifests").mkdir(parents=True)

    result = subprocess.run([str(script), "my-gen-test"], capture_output=True, text=True)
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    config_path = tmp_path / "configs" / "testcases" / "my-gen-test.yaml"
    assert config_path.exists()

    tc = load_testcase(str(config_path))
    assert tc.name == "my-gen-test"
    assert tc.deployment.manifest_path == "my-gen-test.yaml"
    assert tc.deployment.replicas == 1
    assert tc.deployment.resources.gpus == 1
    assert tc.validation.health_port == 8000
    assert tc.validation.test_prompts == ["What is 2+2?"]
    assert tc.validation.metrics_check.check_vllm is True
    assert tc.validation.metrics_check.check_scheduler is True
    assert tc.model.name == "Qwen/Qwen3-0.6B"

    manifest_path = tmp_path / "deploy" / "manifests" / "my-gen-test.yaml"
    assert manifest_path.exists()
    manifest = yaml.safe_load(manifest_path.read_text())
    assert manifest["kind"] == "LLMInferenceService"
    assert manifest["metadata"]["name"] == "my-gen-test"
    assert manifest["spec"]["replicas"] == 1


def test_new_testcase_script_rejects_duplicate(tmp_path, monkeypatch):
    """new-testcase.sh must refuse to overwrite an existing config."""
    import subprocess

    script = Path(__file__).parent.parent / "scripts" / "new-testcase.sh"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs" / "testcases").mkdir(parents=True)
    (tmp_path / "deploy" / "manifests").mkdir(parents=True)

    subprocess.run([str(script), "dupe-test"], capture_output=True, text=True)
    result = subprocess.run([str(script), "dupe-test"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "already exists" in result.stdout or "already exists" in result.stderr
