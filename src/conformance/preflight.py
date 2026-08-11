"""Pre-flight compatibility checks for LLMInferenceService manifests.

Three checks, all dynamic — no hardcoded versions:

  1. **Plugin compatibility** — manifest plugins vs installed EPP
     (live probe from running EPP, or source extract from router repo)
  2. **API version** — manifest apiVersion vs CRD served versions
  3. **Image existence** — EPP image pullable? (skopeo inspect)

Each check degrades safely — skip with warning, never false-fail.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    compatible: bool
    missing_plugins: set[str] = field(default_factory=set)
    required_plugins: set[str] = field(default_factory=set)
    available_count: int = 0
    source: str = ""
    diagnosis: str = ""
    skipped_reason: str = ""


def extract_required_plugins(manifest_path: Path) -> set[str]:
    """Extract plugin type names from a manifest's scheduler config."""
    with open(manifest_path) as f:
        docs = list(yaml.safe_load_all(f))
    manifest = docs[0] if docs else {}

    plugins: set[str] = set()
    # Explicit YAML nulls (e.g. scheduler: null) must not crash — treat like missing.
    spec = manifest.get("spec") or {}
    router = spec.get("router") or {}
    scheduler = router.get("scheduler") or {}
    config = scheduler.get("config") or {}
    inline = config.get("inline") or {}
    for plugin in inline.get("plugins") or []:
        if not isinstance(plugin, dict):
            continue
        ptype = plugin.get("type", "")
        if ptype and not plugin.get("optional", False):
            plugins.add(ptype)

    flow = inline.get("flowControl") or {}
    sat = flow.get("saturationDetector") or {}
    sat_ref = sat.get("pluginRef") or ""
    if sat_ref:
        plugins.add(sat_ref)

    return plugins


def probe_epp_plugins(kubectl_fn, namespace: str) -> frozenset[str] | None:
    """Layer 1: query a running EPP pod for registered plugins."""
    try:
        pods_raw = kubectl_fn(
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/component=llminferenceservice-router-scheduler",
            "--field-selector",
            "status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
            "-n",
            namespace,
            check=False,
        )
        if not pods_raw or not pods_raw.strip():
            return None

        pod = pods_raw.strip().split()[0]
        logs = kubectl_fn("logs", pod, "--tail=200", "-n", namespace, check=False)
        if not logs:
            return None

        plugins = set()
        for line in logs.splitlines():
            if "registered plugin" in line.lower() or "plugin type" in line.lower():
                plugins.update(re.findall(r'"([a-z][a-z0-9-]+)"', line))

        if plugins:
            return frozenset(plugins)
    except Exception:
        pass
    return None


def extract_plugins_from_source(router_repo: str | Path, tag: str) -> frozenset[str]:
    """Layer 2: extract plugin type strings from router Go source at a git tag."""
    result = subprocess.run(
        ["git", "grep", "-h", r'Type\s*\(PluginType\)\?=\s*"', tag, "--", "*.go"],
        capture_output=True,
        text=True,
        cwd=str(router_repo),
    )
    if not result.stdout.strip():
        result = subprocess.run(
            ["git", "grep", "-h", r'Type\s*=\s*"', tag, "--", "*.go"],
            capture_output=True,
            text=True,
            cwd=str(router_repo),
        )

    plugins = set(re.findall(r'"([a-z][a-z0-9-]+)"', result.stdout))
    non_plugins = {
        "content-type",
        "custom",
        "default",
        "colliding-source-type",
        "decode-only",
        "encode-decode",
        "encode-prefill-decode",
        "header-based-testing-filter",
        "destination-endpoint-served-verifier",
    }
    return frozenset(plugins - non_plugins)


def _detect_epp_version_tag(kubectl_fn) -> str:
    """Try to extract a version tag from the installed EPP image reference."""
    for ns in ("redhat-ods-applications", "rhai-gitops", "rhaii"):
        try:
            raw = kubectl_fn(
                "get",
                "llminferenceserviceconfig",
                "-o",
                "jsonpath={.items[0].spec.router.scheduler.template.containers[0].image}",
                "-n",
                ns,
                check=False,
            )
            if not raw or not raw.strip():
                continue
            image = raw.strip().split()[0]
            if ":" in image and "@" not in image:
                tag = image.rsplit(":", 1)[-1]
                if tag.startswith("v"):
                    return tag
        except Exception:
            continue
    return ""


def _list_release_tags(router_path: Path) -> list[str]:
    """Stable release tags in the router checkout (no rc/alpha), sorted ascending."""
    result = subprocess.run(
        ["git", "tag", "--list", "v*.*.*", "--sort=v:refname"],
        capture_output=True,
        text=True,
        cwd=str(router_path),
    )
    return [t.strip() for t in result.stdout.splitlines() if t.strip() and "rc" not in t and "alpha" not in t]


def resolve_available_plugins(
    kubectl_fn,
    namespace: str,
    router_repo: str | Path | None = None,
) -> tuple[frozenset[str] | None, str]:
    """Try both layers to determine available plugins."""
    live = probe_epp_plugins(kubectl_fn, namespace)
    if live:
        return live, f"live probe ({len(live)} plugins)"

    if not router_repo:
        return None, "no detection method available"

    router_path = Path(router_repo)
    if not router_path.exists():
        return None, "no detection method available"

    try:
        epp_tag = _detect_epp_version_tag(kubectl_fn)
        tags = _list_release_tags(router_path)

        if epp_tag:
            plugins = extract_plugins_from_source(router_path, epp_tag)
            if plugins:
                return plugins, f"source extract at {epp_tag} ({len(plugins)} plugins)"
            # Tag present in local repo but extract empty → fail closed (skip).
            # Using "latest" here would false-pass: newer source ≠ cluster EPP.
            if epp_tag in tags:
                log.info(
                    "Tag %s exists locally but yielded no plugins — skipping Layer-2 "
                    "(not falling back to latest; that could false-pass)",
                    epp_tag,
                )
                return None, "no detection method available"
            log.info(
                "Cluster tag %s not in local router tags — falling back to latest",
                epp_tag,
            )

        # Latest-tag fallback: no cluster tag, OR cluster tag absent from local clone.
        if tags:
            latest = tags[-1]
            plugins = extract_plugins_from_source(router_path, latest)
            if plugins:
                if epp_tag:
                    log.warning(
                        "Using latest tag %s — cluster tag %s missing from local router repo",
                        latest,
                        epp_tag,
                    )
                    return (
                        plugins,
                        f"source extract at {latest} (latest — cluster tag {epp_tag} absent locally)",
                    )
                log.warning(
                    "Using latest tag %s — could not determine cluster EPP version",
                    latest,
                )
                return plugins, f"source extract at {latest} (latest — cluster version unknown)"
    except Exception:
        pass

    return None, "no detection method available"


def check_manifest_compatibility(
    manifest_path: Path,
    available_plugins: frozenset[str] | None,
    source: str = "",
) -> PreflightResult:
    """Check if a manifest's plugins are compatible with available plugins."""
    required = extract_required_plugins(manifest_path)

    if not required:
        return PreflightResult(
            compatible=True,
            source=source,
            diagnosis="No custom plugins — uses defaults",
        )

    if available_plugins is None:
        return PreflightResult(
            compatible=True,
            required_plugins=required,
            skipped_reason="could not detect available plugins",
            diagnosis="Pre-flight skipped: no detection method available",
        )

    missing = required - available_plugins

    if not missing:
        return PreflightResult(
            compatible=True,
            required_plugins=required,
            available_count=len(available_plugins),
            source=source,
            diagnosis=f"All {len(required)} required plugins available ({source})",
        )

    return PreflightResult(
        compatible=False,
        missing_plugins=missing,
        required_plugins=required,
        available_count=len(available_plugins),
        source=source,
        diagnosis=(
            f"Manifest requires {len(missing)} plugin(s) not available "
            f"({len(available_plugins)} plugins detected via {source}):\n"
            + "\n".join(f"  - '{p}'" for p in sorted(missing))
            + "\nUse manifests compatible with the installed EPP version."
        ),
    )


# --- Check 2: API version compatibility ---


def check_api_version(manifest_path: Path, kubectl_fn) -> PreflightResult:
    """Check if the manifest's apiVersion is served by the cluster's CRD."""
    with open(manifest_path) as f:
        docs = list(yaml.safe_load_all(f))
    doc = docs[0] if docs else {}
    api_version = doc.get("apiVersion", "")
    version = api_version.rsplit("/", 1)[-1] if "/" in api_version else ""

    if not version:
        return PreflightResult(compatible=True, skipped_reason="no apiVersion in manifest")

    try:
        raw = kubectl_fn(
            "get",
            "crd",
            "llminferenceservices.serving.kserve.io",
            "-o",
            "jsonpath={.spec.versions[?(@.served==true)].name}",
            check=False,
        )
        if not raw or not raw.strip():
            return PreflightResult(compatible=True, skipped_reason="CRD not found")

        served = set(raw.strip().split())
    except Exception:
        return PreflightResult(compatible=True, skipped_reason="could not query CRD versions")

    if version not in served:
        return PreflightResult(
            compatible=False,
            diagnosis=(f"Manifest uses apiVersion '{api_version}' but the cluster CRD only serves {sorted(served)}."),
        )

    return PreflightResult(
        compatible=True,
        diagnosis=f"API version '{version}' is served by the CRD",
    )


# --- Check 3: Image existence ---


def check_image_existence(kubectl_fn) -> PreflightResult:
    """Check if the EPP image referenced in LLMInferenceServiceConfig is pullable."""
    epp_image = ""
    for ns in ("redhat-ods-applications", "rhai-gitops", "rhaii"):
        try:
            raw = kubectl_fn(
                "get",
                "llminferenceserviceconfig",
                "-o",
                "jsonpath={.items[0].spec.router.scheduler.template.containers[0].image}",
                "-n",
                ns,
                check=False,
            )
            if raw and raw.strip():
                epp_image = raw.strip().split()[0]
                break
        except Exception:
            continue

    if not epp_image:
        return PreflightResult(compatible=True, skipped_reason="no EPP image found in config")

    try:
        result = subprocess.run(
            ["skopeo", "inspect", "--raw", f"docker://{epp_image}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[:200].lower()
            if "manifest unknown" in stderr or "not found" in stderr:
                return PreflightResult(
                    compatible=False,
                    diagnosis=(
                        f"EPP image '{epp_image}' does not exist: {result.stderr.strip()[:200]}\n"
                        f"Verify the image was pushed to the registry."
                    ),
                )
            return PreflightResult(
                compatible=True,
                skipped_reason=f"image check inconclusive: {result.stderr.strip()[:100]}",
            )
    except FileNotFoundError:
        return PreflightResult(compatible=True, skipped_reason="skopeo not installed")
    except Exception as e:
        return PreflightResult(compatible=True, skipped_reason=f"image check failed: {e}")

    return PreflightResult(
        compatible=True,
        diagnosis=f"EPP image '{epp_image.split('/')[-1][:50]}' exists and is pullable",
    )
