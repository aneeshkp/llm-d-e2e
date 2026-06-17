# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

End-to-end conformance test suite for [llm-d](https://github.com/llm-d) / KServe `LLMInferenceService` deployments on Kubernetes. Python + pytest framework that deploys LLMInferenceService resources and validates them through ordered phases (CRD check, deploy, service/gateway/pod readiness, health, inference, metrics, cleanup).

## Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/) package manager
- `kubectl` configured with cluster access (for conformance tests, not unit tests)
- Cluster with `LLMInferenceService` CRD installed (RHAI or KServe)
- Manifests from [llm-d-conformance-manifests](https://github.com/aneeshkp/llm-d-conformance-manifests) (cloned via `--setup`)

## Common Commands

```bash
uv sync                                              # install dependencies
uv run llm-d-e2e --setup main                        # clone test manifests (latest)
uv run llm-d-e2e --setup 3.4-stable                  # clone manifests (specific branch)

uv run pytest tests/test_smoke.py -v                  # unit tests (no cluster needed)
uv run ruff check src/ tests/                         # lint
uv run ruff format src/ tests/                        # format

uv run llm-d-e2e -t single-gpu-smoke                  # run single conformance test case
uv run llm-d-e2e -t single-gpu,cache-aware            # run multiple test cases
uv run llm-d-e2e -t single-gpu --mock                                    # simulate vLLM (no GPU)
uv run llm-d-e2e -t single-gpu --mode discover --endpoint http://svc:8000 # validate existing deployment
uv run llm-d-e2e -p configs/profiles/smoke.yaml       # run a profile
uv run llm-d-e2e -t single-gpu --nocleanup            # keep resources after test
uv run llm-d-e2e --list-testcases                     # list available test cases
uv run llm-d-e2e --list-profiles                      # list available profiles
```

Makefile targets mirror CLI: `make test TESTCASE=single-gpu`, `make unittest`, `make lint`, `make format`, `make setup`.

## Architecture

### CLI → pytest delegation

`cli.py:main()` parses user flags and translates them to pytest options, then runs `pytest tests/test_conformance.py` as a subprocess. **Every CLI flag in `cli.py` must have a matching `conftest.py:pytest_addoption()` entry** — when adding a new flag, update both files and the `flag_map` dict in `cli.py:main()`. Boolean flags (`--nocleanup`, `--disable-auth`) are handled separately after `flag_map` iteration since they use `store_true` rather than values.

### Test case parametrization

`conftest.py:pytest_generate_tests()` resolves which test cases to run (from `--testcase` names or `--profile` YAML) and parametrizes the `tc` fixture. Each `tc` is a `TestCase` dataclass loaded from `configs/testcases/*.yaml`. `TestConformance` methods run once per test case.

### Ordered conformance phases

`test_conformance.py:TestConformance` uses numeric method name prefixes (`test_01_` through `test_99_`) for phase ordering: prereq → deploy → service → gateway → pods → ready → health → models → inference → metrics (4 variants) → cleanup. Phases skip themselves based on `tc` config flags or `--mode discover`.

### Fixture scoping

- **Session-scoped**: `deployer` (one kubectl wrapper per run), `report` (finalized at session end)
- **Class-scoped**: `endpoint` (resolved per test case via port-forward or explicit URL), `client`, `scraper`

### Source modules (`src/conformance/`)

- **config.py** — Dataclass config types and YAML loaders. YAML keys are camelCase, Python fields are snake_case; `_build()` handles recursive conversion.
- **deployer.py** — `Deployer`: manages LLMInferenceService lifecycle via `kubectl` subprocess calls. Handles deploy, wait-for-ready, port-forwarding, manifest patching (mock image, pull secrets, auth disable), and cleanup. All cluster interaction is subprocess `kubectl` — no Python K8s client.
- **client.py** — `LLMClient`: OpenAI-compatible HTTP client (httpx) for `/health`, `/v1/models`, `/v1/completions`, `/v1/chat/completions`.
- **metrics.py** — `Scraper`: scrapes Prometheus metrics from pods via `kubectl exec`. `parse_prometheus()` parses text exposition format. Per-topology validators: `validate_vllm_basic`, `validate_cache_aware`, `validate_pd`, `validate_scheduler`.
- **model.py** — `ModelDownloader`: creates PVCs and download Jobs for pre-caching models from HuggingFace.
- **report.py** — JSON report generation with pass/fail/skip summary.

### Model caching (`--model-source pvc`)

`model.py:ModelDownloader` creates a PVC and a K8s Job that downloads a HuggingFace model into it. When `--model-source pvc` is set, `deployer.py` switches the model URI from `hf://` to `pvc://` and patches the manifest accordingly. The PVC can be retained across runs (`cache.keepPVC: true` in test case YAML) to avoid re-downloading.

### Config files

- **configs/testcases/*.yaml** — Each file maps to one `TestCase` dataclass. Contains model info, deployment spec (manifest path, replicas, resources, timeouts), validation criteria (prompts, retry config), and metrics check flags.
- **configs/profiles/*.yaml** — Named groups of test case names (e.g., `smoke`, `all`, `pd`).
- **deploy/manifests/*.yaml** — LLMInferenceService manifests, cloned from [llm-d-conformance-manifests](https://github.com/aneeshkp/llm-d-conformance-manifests) via `--setup`. Gitignored.

### vLLM Simulator (`--mock`)

The `--mock` flag replaces the vLLM container with [llm-d-inference-sim](https://github.com/llm-d/llm-d-inference-sim) (`ghcr.io/llm-d/llm-d-inference-sim:latest`), a Go-based simulator with OpenAI-compatible endpoints, vLLM-compatible Prometheus metrics, configurable latency, and KV cache simulation. When `--mock` is used, `deployer.py:_replace_vllm_image()` patches the manifest to: swap the container image, inject simulator args (`--model`, `--port`, `--self-signed-certs`, `--mode random`), and strip GPU resource requests. A custom image can be passed: `--mock my-image:v1`.

## Adding a New Test Case

1. Create `configs/testcases/<name>.yaml` using camelCase keys matching the `TestCase` dataclass hierarchy in `config.py`.
2. Add the corresponding LLMInferenceService manifest to the manifest repo (or `deploy/manifests/` for local testing). Set `deployment.manifestPath` in the YAML to the filename.
3. Enable the appropriate `metricsCheck` flags (`checkVLLM`, `checkScheduler`, `checkPrefixCache`, `checkPD`) based on the deployment topology.
4. Add the test case name to relevant profiles in `configs/profiles/*.yaml`.

## Adding a New Config Field

1. Add the dataclass field in `config.py` (snake_case).
2. If it's a new nested type, add a `_build()` branch for it (matching on the type name string in hints).
3. Use camelCase for the key in YAML files.
4. Duration fields (named `timeout`, `ready_timeout`, `retry_interval`) are auto-parsed from strings like `"15m"`, `"2h"`, `"300s"`.

## Adding a New Conformance Phase

1. Add a method `test_NN_<name>` to `TestConformance` in `test_conformance.py`. Pick a number between existing phases.
2. Use `pytest.skip()` for conditions where the phase doesn't apply (e.g., discover mode, disabled config flag).
3. Phases receive fixtures via parameter names: `deployer`, `tc`, `client`, `endpoint`, `scraper`, `test_mode`, `no_cleanup`.

## Metrics Validation Topology

Each metrics validator in `metrics.py` targets a specific deployment topology:

| Validator               | Scrape target | Test phase | Topology          |
|------------------------|---------------|------------|--------------------|
| `validate_vllm_basic`  | workload pods | test_10    | All (basic check)  |
| `validate_cache_aware` | workload + EPP | test_11   | Prefix KV cache    |
| `validate_pd`          | workload + prefill | test_12 | P/D disaggregation |
| `validate_scheduler`   | EPP pods      | test_13    | Scheduler/EPP      |

EPP pod discovery tries multiple label patterns (`EPP_LABELS` list in `metrics.py`) because the component label varies across llm-d versions.

## Key Design Decisions

- All cluster interaction goes through `kubectl` subprocess calls (no Python K8s client library).
- Test cases are data-driven via YAML configs, not hardcoded in test files.
- The `--mock` flag swaps the vLLM container with llm-d-inference-sim, injects simulator args, and strips GPU resource requests, enabling full e2e flow without GPUs.
- camelCase in YAML, snake_case in Python — `_snake()` and `_build()` in `config.py` bridge the two.

## Code Style

- Ruff for linting and formatting, line length 120, target Python 3.11.
- Uses `from __future__ import annotations` throughout for PEP 604 union syntax.
- Config types are plain dataclasses (no Pydantic).
