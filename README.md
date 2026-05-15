# llm-d-e2e

End-to-end conformance tests for [llm-d](https://github.com/llm-d) / KServe `LLMInferenceService` deployments on Kubernetes.

Python + pytest rewrite of the [Go/Ginkgo conformance framework](https://github.com/aneeshkp/llm-d-conformance-test). See that project's [architecture docs](https://github.com/aneeshkp/llm-d-conformance-test/blob/main/docs/architecture.md) for detailed diagrams, test topologies, and metrics coverage.

## Quick Start

```bash
# Install
uv sync

# Setup manifests
uv run llm-d-e2e --setup main

# Run smoke test
uv run llm-d-e2e -t single-gpu-smoke

# Run all conformance tests
uv run llm-d-e2e -p configs/profiles/all.yaml

# List available tests
uv run llm-d-e2e --list-testcases
uv run llm-d-e2e --list-profiles
```

## Usage

```bash
# Single test case
uv run llm-d-e2e -t single-gpu

# Multiple test cases
uv run llm-d-e2e -t single-gpu,cache-aware

# With options
uv run llm-d-e2e -t single-gpu --nocleanup          # keep resources for debugging
uv run llm-d-e2e -t single-gpu --model-source pvc    # use pre-cached model
uv run llm-d-e2e -t single-gpu --mock <image>        # mock vLLM (no GPU)
uv run llm-d-e2e -t single-gpu -v -x                 # verbose, fail-fast
uv run llm-d-e2e -t single-gpu --html report.html    # HTML report
```

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
```
