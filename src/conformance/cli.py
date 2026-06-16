"""CLI entry point for llm-d-e2e conformance tests."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_MOCK_IMAGE = "ghcr.io/llm-d/llm-d-inference-sim:latest"


def main():
    parser = argparse.ArgumentParser(
        prog="llm-d-e2e",
        description="End-to-end conformance tests for llm-d / KServe LLMInferenceService",
    )

    # Test selection
    parser.add_argument("--testcase", "-t", default="", help="Test case name(s), comma-separated")
    parser.add_argument("--profile", "-p", default="", help="Profile YAML path")
    parser.add_argument("--testcase-dir", default="configs/testcases", help="Test case directory")

    # Cluster
    parser.add_argument("--platform", default="any", choices=["any", "ocp", "aks", "gks"], help="Platform")
    parser.add_argument("--namespace", "-n", default="llm-conformance-test", help="Kubernetes namespace")
    parser.add_argument("--kubeconfig", default="", help="Path to kubeconfig")

    # Mode
    parser.add_argument("--mode", default="deploy", choices=["deploy", "discover", "cache"], help="Run mode")
    parser.add_argument("--model-source", default="hf", choices=["hf", "pvc"], help="Model source")
    parser.add_argument("--model", default="", help="Override model name")
    parser.add_argument("--endpoint", default="", help="Service URL for discover mode")
    parser.add_argument(
        "--mock", default="", nargs="?", const=DEFAULT_MOCK_IMAGE,
        help=f"Use simulator image (no GPU needed). Default: {DEFAULT_MOCK_IMAGE}",
    )
    parser.add_argument("--render-image", default="", help="vLLM CPU image for tokenizer render sidecar (used with --mock)")

    # Auth
    parser.add_argument("--pull-secret", default="", help="Pull secret name")
    parser.add_argument("--bearer-token", default="", help="Bearer token for auth")
    parser.add_argument("--disable-auth", action="store_true", help="Disable WASM auth annotation")

    # Storage
    parser.add_argument("--storage-class", default="", help="StorageClass for PVC")
    parser.add_argument("--storage-size", default="", help="Override PVC size")

    # Behavior
    parser.add_argument("--nocleanup", action="store_true", help="Keep resources after test")
    parser.add_argument("--report-dir", default="reports", help="Report output directory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--fail-fast", "-x", action="store_true", help="Stop on first failure")
    parser.add_argument("--html", default="", help="Generate HTML report at path")

    # Utility subcommands
    parser.add_argument("--list-testcases", action="store_true", help="List available test cases")
    parser.add_argument("--list-profiles", action="store_true", help="List available profiles")
    parser.add_argument("--setup", default="", metavar="REF", nargs="?", const="main",
                        help="Clone manifest repo (default branch: main)")

    args = parser.parse_args()

    # Handle utility commands
    if args.list_testcases:
        _list_testcases(args.testcase_dir)
        return

    if args.list_profiles:
        _list_profiles()
        return

    if args.setup is not None and args.setup != "":
        _setup_manifests(args.setup)
        return

    # Build pytest args
    pytest_args = ["tests/test_conformance.py"]

    # Pass all flags through to pytest
    flag_map = {
        "testcase": "--testcase",
        "profile": "--profile",
        "testcase_dir": "--testcase-dir",
        "platform": "--platform",
        "namespace": "--namespace",
        "kubeconfig": "--kubeconfig",
        "mode": "--mode",
        "model_source": "--model-source",
        "model": "--model",
        "endpoint": "--endpoint",
        "mock": "--mock",
        "render_image": "--render-image",
        "pull_secret": "--pull-secret",
        "bearer_token": "--bearer-token",
        "storage_class": "--storage-class",
        "storage_size": "--storage-size",
        "report_dir": "--report-dir",
    }

    for attr, flag in flag_map.items():
        val = getattr(args, attr)
        if val:
            pytest_args.extend([flag, val])

    if args.disable_auth:
        pytest_args.append("--disable-auth")
    if args.nocleanup:
        pytest_args.append("--nocleanup")
    if args.verbose:
        pytest_args.append("-v")
    if args.fail_fast:
        pytest_args.append("-x")
    if args.html:
        pytest_args.extend(["--html", args.html, "--self-contained-html"])

    pytest_args.extend(["--tb", "short", "--timeout", "21600"])

    sys.exit(subprocess.call(["python", "-m", "pytest"] + pytest_args))


def _list_testcases(testcase_dir: str):
    import yaml
    print("Test cases:")
    for f in sorted(Path(testcase_dir).glob("*.yaml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
        name = data.get("name", f.stem)
        desc = data.get("description", "")
        gpus = data.get("deployment", {}).get("resources", {}).get("gpus", "?")
        print(f"  {name:<28s} [{gpus} GPU]  {desc}")


def _list_profiles():
    import yaml
    print("Profiles:")
    for f in sorted(Path("configs/profiles").glob("*.yaml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
        name = data.get("name", f.stem)
        desc = data.get("description", "")
        cases = ", ".join(data.get("testCases", []))
        print(f"  {name:<20s} {desc}")
        print(f"  {'':20s} tests: {cases}")


def _setup_manifests(ref: str):
    repo = "https://github.com/aneeshkp/llm-d-conformance-manifests.git"
    manifest_dir = Path("deploy/manifests")
    manifest_dir.mkdir(parents=True, exist_ok=True)

    print(f"Cloning manifests from {ref}...")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, repo, "/tmp/llm-d-manifests"],
        capture_output=True,
    )
    for f in Path("/tmp/llm-d-manifests").glob("*.yaml"):
        (manifest_dir / f.name).write_text(f.read_text())
    subprocess.run(["rm", "-rf", "/tmp/llm-d-manifests"])
    print(f"Manifests ready in {manifest_dir}/")


if __name__ == "__main__":
    main()
