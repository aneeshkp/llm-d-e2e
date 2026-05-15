"""Kubernetes deployer for LLMInferenceService resources."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from conformance.config import TestCase

log = logging.getLogger(__name__)

WORKLOAD_LABEL = "app.kubernetes.io/name={name},app.kubernetes.io/component=llminferenceservice-workload"
PREFILL_LABEL = "app.kubernetes.io/name={name},app.kubernetes.io/component=llminferenceservice-workload-prefill"


@dataclass
class DeployResult:
    name: str = ""
    namespace: str = ""
    success: bool = False
    error: str = ""
    duration: float = 0.0
    logs: list[str] = field(default_factory=list)


class Deployer:
    """Manages deploy, wait, and cleanup of LLMInferenceService resources via kubectl."""

    def __init__(
        self,
        kubeconfig: str = "",
        platform: str = "any",
        namespace: str = "llm-conformance-test",
        model_source: str = "hf",
        mock_image: str = "",
        pull_secret: str = "",
        disable_auth: bool = False,
        manifest_dir: str = "deploy/manifests",
    ):
        self.kubeconfig = kubeconfig
        self.platform = platform
        self.namespace = namespace
        self.model_source = model_source
        self.mock_image = mock_image
        self.pull_secret = pull_secret
        self.disable_auth = disable_auth
        self.manifest_dir = Path(manifest_dir)
        self._port_forward_proc: subprocess.Popen | None = None
        self._port_forward_port: int = 0

    def kubectl(self, *args: str, check: bool = True) -> str:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        cmd += list(args)
        log.debug("kubectl %s", " ".join(args))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if check and result.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def ensure_namespace(self):
        try:
            self.kubectl("get", "namespace", self.namespace, check=True)
        except RuntimeError:
            self.kubectl("create", "namespace", self.namespace)

    def check_crd_exists(self, crd_name: str) -> bool:
        try:
            self.kubectl("get", "crd", crd_name)
            return True
        except RuntimeError:
            return False

    def check_resource_exists(self, kind: str, name: str) -> bool:
        try:
            self.kubectl("get", kind, name, "-n", self.namespace)
            return True
        except RuntimeError:
            return False

    def deploy(self, tc: TestCase) -> DeployResult:
        start = time.time()
        result = DeployResult(name=tc.name, namespace=self.namespace)

        manifest_path = self.manifest_dir / tc.deployment.manifest_path
        if not manifest_path.exists():
            result.error = f"Manifest not found: {manifest_path}"
            return result

        manifest = self._patch_manifest(manifest_path, tc)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(manifest, f)
            tmp_path = f.name

        try:
            self.ensure_namespace()
            self.kubectl("apply", "-n", self.namespace, "-f", tmp_path)
            result.success = True
        except RuntimeError as e:
            result.error = str(e)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            result.duration = time.time() - start

        return result

    def wait_for_ready(self, tc: TestCase, timeout: float | None = None, print_fn=None) -> bool:
        if timeout is None:
            timeout = tc.deployment.ready_timeout.total_seconds()
        deadline = time.time() + timeout
        name = tc.name
        start = time.time()

        while time.time() < deadline:
            elapsed = int(time.time() - start)
            try:
                status = self.kubectl(
                    "get", "llminferenceservice", name, "-n", self.namespace,
                    "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                    check=False,
                )
                reason = self.kubectl(
                    "get", "llminferenceservice", name, "-n", self.namespace,
                    "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].reason}",
                    check=False,
                ) or "waiting"
                if print_fn:
                    print_fn(f"[{elapsed}s/{int(timeout)}s] Ready={status or 'Unknown'} reason={reason}")
                if status == "True":
                    return True
            except RuntimeError:
                if print_fn:
                    print_fn(f"[{elapsed}s/{int(timeout)}s] resource not found yet")
            time.sleep(15)

        raise TimeoutError(f"{name} not ready after {timeout}s")

    def wait_for_service(self, name: str, timeout: float = 300) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                output = self.kubectl(
                    "get", "svc", "-n", self.namespace,
                    "-l", f"app.kubernetes.io/name={name}",
                    "-o", "jsonpath={.items[0].metadata.name}",
                    check=False,
                )
                if output:
                    return output
            except RuntimeError:
                pass
            time.sleep(10)
        raise TimeoutError(f"Service for {name} not found after {timeout}s")

    def wait_for_httproute(self, name: str, timeout: float = 300) -> bool:
        deadline = time.time() + timeout
        route_name = f"{name}-kserve-route"
        while time.time() < deadline:
            if self.check_resource_exists("httproute", route_name):
                return True
            time.sleep(10)
        raise TimeoutError(f"HTTPRoute {route_name} not found after {timeout}s")

    def wait_for_gateway(self, timeout: float = 300) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                output = self.kubectl(
                    "get", "gateway", "-A",
                    "-o", "jsonpath={.items[0].status.addresses[0].value}",
                    check=False,
                )
                if output:
                    return output
            except RuntimeError:
                pass
            time.sleep(10)
        raise TimeoutError(f"Gateway not programmed after {timeout}s")

    def wait_for_pods(self, name: str, timeout: float = 600, print_fn=None) -> list[str]:
        label = WORKLOAD_LABEL.format(name=name)
        deadline = time.time() + timeout
        start = time.time()
        while time.time() < deadline:
            elapsed = int(time.time() - start)
            try:
                output = self.kubectl(
                    "get", "pods", "-n", self.namespace,
                    "-l", label, "-o", "jsonpath={range .items[*]}{.metadata.name}={.status.phase} {end}",
                    check=False,
                )
                pod_statuses = output.strip().split() if output.strip() else []
                if print_fn and pod_statuses:
                    print_fn(f"[{elapsed}s/{int(timeout)}s] pods: {', '.join(pod_statuses)}")
                elif print_fn:
                    print_fn(f"[{elapsed}s/{int(timeout)}s] no pods found yet")

                pods = []
                all_running = True
                for ps in pod_statuses:
                    parts = ps.split("=")
                    if len(parts) == 2:
                        pods.append(parts[0])
                        if parts[1] != "Running":
                            all_running = False
                if pods and all_running:
                    return pods
            except RuntimeError:
                if print_fn:
                    print_fn(f"[{elapsed}s/{int(timeout)}s] waiting for pods...")
            time.sleep(15)
        raise TimeoutError(f"Pods for {name} not running after {timeout}s")

    def wait_for_inference_pool(self, name: str, timeout: float = 300) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                output = self.kubectl(
                    "get", "inferencepool", "-n", self.namespace,
                    "-o", "jsonpath={.items[*].metadata.name}",
                    check=False,
                )
                if output:
                    return True
            except RuntimeError:
                pass
            time.sleep(10)
        raise TimeoutError(f"InferencePool for {name} not found after {timeout}s")

    def get_endpoint(self, name: str) -> str:
        try:
            url = self.kubectl(
                "get", "llminferenceservice", name, "-n", self.namespace,
                "-o", "jsonpath={.status.url}",
            )
            if url:
                path = url.split("//", 1)[-1].split("/", 1)
                path_suffix = f"/{path[1]}" if len(path) > 1 else f"/{self.namespace}/{name}"
                local_url = self._ensure_port_forward(path_suffix)
                if local_url:
                    return local_url
                return url
        except RuntimeError:
            pass
        gateway_addr = self.wait_for_gateway()
        return f"http://{gateway_addr}/{self.namespace}/{name}"

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def _ensure_port_forward(self, path: str) -> str | None:
        if self._port_forward_proc and self._port_forward_proc.poll() is None:
            return f"http://localhost:{self._port_forward_port}{path}"

        local_port = self._find_free_port()
        gateway_svc = "svc/inference-gateway-istio"
        gateway_ns = "redhat-ods-applications"

        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        cmd += ["port-forward", "-n", gateway_ns, gateway_svc, f"{local_port}:80"]

        log.info("Starting port-forward: localhost:%d → %s:80", local_port, gateway_svc)
        self._port_forward_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._port_forward_port = local_port

        time.sleep(3)
        if self._port_forward_proc.poll() is not None:
            log.warning("Port-forward failed to start")
            self._port_forward_proc = None
            return None

        return f"http://localhost:{local_port}{path}"

    def stop_port_forward(self):
        if self._port_forward_proc:
            self._port_forward_proc.terminate()
            self._port_forward_proc.wait(timeout=5)
            self._port_forward_proc = None
            log.info("Port-forward stopped")

    def cleanup(self, tc: TestCase, timeout: float = 120):
        log.info("Cleaning up %s", tc.name)
        self.kubectl(
            "delete", "llminferenceservice", tc.name, "-n", self.namespace,
            "--timeout", f"{int(timeout)}s", "--ignore-not-found",
            check=False,
        )
        deadline = time.time() + timeout
        label = WORKLOAD_LABEL.format(name=tc.name)
        while time.time() < deadline:
            output = self.kubectl(
                "get", "pods", "-n", self.namespace, "-l", label,
                "-o", "jsonpath={.items[*].metadata.name}", check=False,
            )
            if not output.strip():
                return
            time.sleep(5)

    def get_platform_info(self) -> dict:
        info = {"platform": self.platform}
        try:
            info["k8s_version"] = self.kubectl("version", "--short", "--client", check=False)
        except RuntimeError:
            pass
        return info

    def _patch_manifest(self, path: Path, tc: TestCase) -> dict:
        with open(path) as f:
            manifest = yaml.safe_load(f)

        spec = manifest.get("spec", {})

        if tc.model.uri:
            model = spec.setdefault("model", {})
            model["uri"] = tc.model.uri
            model["name"] = tc.model.name

        if self.mock_image:
            self._replace_vllm_image(spec, self.mock_image)

        if self.pull_secret:
            self._inject_pull_secret(spec, self.pull_secret)

        if self.disable_auth:
            annotations = manifest.setdefault("metadata", {}).setdefault("annotations", {})
            annotations["serving.kserve.io/disable-auth"] = "true"

        return manifest

    def _replace_vllm_image(self, spec: dict, image: str):
        for template_key in ("template", "prefill"):
            template = spec.get(template_key, {})
            for container in template.get("containers", []):
                if container.get("name") == "main":
                    container["image"] = image

    def _inject_pull_secret(self, spec: dict, secret_name: str):
        for template_key in ("template", "prefill"):
            template = spec.get(template_key, {})
            if template:
                secrets = template.setdefault("imagePullSecrets", [])
                if not any(s.get("name") == secret_name for s in secrets):
                    secrets.append({"name": secret_name})
