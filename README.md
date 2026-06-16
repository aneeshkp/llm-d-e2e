# llm-d-e2e

End-to-end conformance tests for [llm-d](https://github.com/llm-d) / KServe `LLMInferenceService` deployments on Kubernetes.

Python + pytest rewrite of the [Go/Ginkgo conformance framework](https://github.com/aneeshkp/llm-d-conformance-test). See that project's [architecture docs](https://github.com/aneeshkp/llm-d-conformance-test/blob/main/docs/architecture.md) for detailed diagrams, test topologies, and metrics coverage.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- `kubectl` configured with cluster access
- Cluster with `LLMInferenceService` CRD installed (RHAI or KServe)

Install uv if you don't have it:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/aneeshkp/llm-d-e2e.git
cd llm-d-e2e

# 2. Install dependencies
uv sync

# 3. Clone test manifests (LLMInferenceService YAMLs)
uv run llm-d-e2e --setup main          # latest
uv run llm-d-e2e --setup 3.4-stable    # specific release

# 4. (Optional) Set up a shortcut
alias e2e='uv run llm-d-e2e'
```

## Quick Start

```bash
# List available tests and profiles
e2e --list-testcases
e2e --list-profiles

# Run smoke test
e2e -t single-gpu-smoke

# Run all conformance tests
e2e -p configs/profiles/all.yaml
```

## Usage

```bash
# Single test case
e2e -t single-gpu

# Multiple test cases
e2e -t single-gpu,cache-aware

# Keep resources after test (for debugging)
e2e -t single-gpu --nocleanup

# Use pre-cached model from PVC
e2e -t single-gpu --model-source pvc

# Simulate vLLM with llm-d-inference-sim (no GPU needed)
e2e -t single-gpu --mock

# Verbose output, stop on first failure
e2e -t single-gpu -v -x

# Generate HTML report
e2e -t single-gpu --html report.html

# Target a specific cluster
e2e -t single-gpu --kubeconfig ~/.kube/config --namespace my-test-ns

# Validate an existing deployment (no deploy/cleanup)
e2e -t single-gpu --mode discover --endpoint http://my-service:8000
```

## Testing an Existing Deployment

If you already have an LLMInferenceService running and just want to validate it (health, inference, metrics) without deploying or cleaning up:

```bash
# Get the service URL
kubectl get llminferenceservice -n my-namespace
# NAME         URL                                    READY
# my-model     http://gateway.example.com/ns/model    True

# Run validation against it
e2e -t single-gpu --mode discover --namespace my-namespace

# Or specify the endpoint directly
e2e -t single-gpu --mode discover --endpoint http://gateway.example.com/ns/model
```

This skips the deploy and cleanup phases — only runs health, models, inference, and metrics checks.

## Test Cases

| Name | GPUs | What it tests |
|------|------|---------------|
| single-gpu-smoke | 1 | Fast baseline (no metrics) |
| single-gpu | 1 | Scheduler + metrics |
| single-gpu-no-scheduler | 3 | K8s native round-robin |
| cache-aware | 2 | Prefix KV cache routing |
| pd | 3 | Prefill/Decode disaggregation |
| pd-cache-aware | 3 | P/D + prefix cache |
| moe | 8 | MoE, RDMA, expert parallelism |
| multi-pool | 2 | Multiple InferencePools |

## Test Phases

Each test case runs through ordered phases:

1. **Prereq** — CRD exists
2. **Deploy** — apply LLMInferenceService manifest
3. **Service** — wait for Service
4. **Gateway** — wait for Gateway programmed
5. **Pods** — wait for pods Running
6. **Ready** — wait for Ready=True
7. **Health** — GET /health
8. **Models** — GET /v1/models
9. **Inference** — POST /v1/chat/completions
10. **Metrics** — scrape and validate Prometheus metrics
11. **Cleanup** — delete resources

## Development

```bash
# Run unit tests (no cluster needed)
uv run pytest tests/test_smoke.py -v

# Lint
uv run ruff check src/ tests/

# Format
uv run ruff format src/ tests/
```
