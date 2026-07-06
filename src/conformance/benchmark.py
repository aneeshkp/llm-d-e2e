"""GuideLLM benchmark runner — creates a K8s Job, waits for it, parses results."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

GUIDELLM_JOB_TEMPLATE = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: {namespace}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: guidellm
        image: {image}
        command: ["/bin/bash", "-c"]
        args:
        - |
          {command}
        env:
        - name: USER
          value: guidellm
        volumeMounts:
        - name: home
          mountPath: /home/guidellm
        - name: results
          mountPath: /results
      volumes:
      - name: home
        emptyDir: {{}}
      - name: results
        emptyDir: {{}}
"""

RESULT_MARKER = "---GUIDELLM_JSON_START---"


@dataclass
class BenchmarkResult:
    output_tokens_per_second: float = 0.0
    ttft_median: float = 0.0
    ttft_p95: float = 0.0
    itl_median: float = 0.0
    itl_p95: float = 0.0
    total_requests: int = 0
    completed_requests: int = 0
    failed_requests: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _build_command(
    target_url: str, model: str, rate: int, max_seconds: int, data: str, backend_type: str, request_type: str
) -> str:
    model_arg = f" --model={model}" if model else ""
    return (
        f"/opt/app-root/bin/guidellm benchmark"
        f" --target={target_url}"
        f"{model_arg}"
        f" --profile=concurrent"
        f" --rate={rate}"
        f" --max-seconds={max_seconds}"
        f" --data='{data}'"
        f" --backend-type={backend_type}"
        f" --request-type={request_type}"
        f" --outputs=json"
        f" && echo '{RESULT_MARKER}'"
        f" && cat /results/benchmarks.json"
    )


def run_benchmark(
    kubectl_fn: Callable[..., str],
    namespace: str,
    target_url: str,
    model: str,
    image: str,
    rate: int = 100,
    max_seconds: int = 240,
    data: str = "",
    backend_type: str = "openai_http",
    request_type: str = "text_completions",
    timeout: float = 1800,
    job_name: str = "guidellm-benchmark",
    print_fn: Callable[[str], None] | None = None,
) -> BenchmarkResult:
    """Create a GuideLLM benchmark Job, wait for completion, and parse results."""
    command = _build_command(target_url, model, rate, max_seconds, data, backend_type, request_type)
    job_yaml = GUIDELLM_JOB_TEMPLATE.format(
        name=job_name,
        namespace=namespace,
        image=image,
        command=command,
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(job_yaml)
        tmp_path = f.name

    try:
        kubectl_fn("delete", "job", job_name, "-n", namespace, "--ignore-not-found", check=False)
        kubectl_fn("apply", "-f", tmp_path)
        if print_fn:
            print_fn(f"Created benchmark job '{job_name}'")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    _wait_for_job(kubectl_fn, job_name, namespace, timeout, print_fn)
    logs = _get_job_logs(kubectl_fn, job_name, namespace)
    result = parse_benchmark_results(logs)

    if print_fn:
        print_fn(
            f"output_tokens/s={result.output_tokens_per_second:.1f}, "
            f"TTFT median={result.ttft_median:.1f}ms p95={result.ttft_p95:.1f}ms, "
            f"ITL median={result.itl_median:.2f}ms p95={result.itl_p95:.2f}ms, "
            f"{result.completed_requests}/{result.total_requests} completed, "
            f"{result.failed_requests} failed"
        )

    return result


def _wait_for_job(
    kubectl_fn: Callable[..., str],
    name: str,
    namespace: str,
    timeout: float,
    print_fn: Callable[[str], None] | None = None,
) -> None:
    deadline = time.time() + timeout
    start = time.time()
    while time.time() < deadline:
        elapsed = int(time.time() - start)
        status = kubectl_fn(
            "get",
            "job",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Complete')].status}",
            check=False,
        )
        if status == "True":
            if print_fn:
                print_fn(f"Job completed in {elapsed}s")
            return

        failed = kubectl_fn(
            "get",
            "job",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Failed')].status}",
            check=False,
        )
        if failed == "True":
            reason = kubectl_fn(
                "get",
                "job",
                name,
                "-n",
                namespace,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Failed')].reason}",
                check=False,
            )
            raise RuntimeError(f"Benchmark job '{name}' failed: {reason}")

        if print_fn:
            print_fn(f"[{elapsed}s/{int(timeout)}s] waiting for benchmark job...")
        time.sleep(30)

    raise TimeoutError(f"Benchmark job '{name}' not complete after {timeout}s")


def _get_job_logs(kubectl_fn: Callable[..., str], name: str, namespace: str) -> str:
    pod_name = kubectl_fn(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        f"job-name={name}",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )
    if not pod_name:
        raise RuntimeError(f"No pod found for job {name}")
    return kubectl_fn("logs", "-n", namespace, pod_name, "-c", "guidellm", check=False)


def parse_benchmark_results(pod_logs: str) -> BenchmarkResult:
    """Parse GuideLLM JSON results from pod logs using the marker."""
    if RESULT_MARKER not in pod_logs:
        log.warning("GuideLLM result marker not found in logs")
        return BenchmarkResult()

    json_text = pod_logs.split(RESULT_MARKER, 1)[1].strip()
    if not json_text:
        log.warning("No JSON content after marker")
        return BenchmarkResult()

    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError as e:
        log.error("Failed to parse GuideLLM JSON: %s", e)
        return BenchmarkResult()

    benchmarks = raw.get("benchmarks", [])
    if not benchmarks:
        log.warning("No benchmarks in GuideLLM output")
        return BenchmarkResult(raw=raw)

    bench = benchmarks[-1]
    metrics = bench.get("metrics", {})

    request_totals = metrics.get("request_totals", {})
    ttft = metrics.get("time_to_first_token_ms", {}).get("successful", {})
    ttft_percentiles = ttft.get("percentiles", {})
    itl = metrics.get("inter_token_latency_ms", {}).get("successful", {})
    itl_percentiles = itl.get("percentiles", {})
    otps = metrics.get("output_tokens_per_second", {}).get("successful", {})

    return BenchmarkResult(
        output_tokens_per_second=float(otps.get("mean", 0.0)),
        ttft_median=float(ttft.get("median", 0.0)),
        ttft_p95=float(ttft_percentiles.get("p95", 0.0)),
        itl_median=float(itl.get("median", 0.0)),
        itl_p95=float(itl_percentiles.get("p95", 0.0)),
        total_requests=int(request_totals.get("total", 0)),
        completed_requests=int(request_totals.get("successful", 0)),
        failed_requests=int(request_totals.get("errored", 0)),
        raw=raw,
    )
