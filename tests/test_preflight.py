"""Tests for pre-flight plugin compatibility — no static registry, no hardcoded versions."""

from __future__ import annotations

from pathlib import Path

import yaml

from conformance.preflight import (
    check_api_version,
    check_image_existence,
    check_manifest_compatibility,
    extract_plugins_from_source,
    extract_required_plugins,
    probe_epp_plugins,
    resolve_available_plugins,
)


def _write_manifest(tmp_path: Path, plugins: list[dict], extras: dict | None = None) -> Path:
    inline = {"plugins": plugins}
    if extras:
        inline.update(extras)
    manifest = {
        "apiVersion": "serving.kserve.io/v1alpha2",
        "kind": "LLMInferenceService",
        "metadata": {"name": "test"},
        "spec": {"model": {"name": "m"}, "router": {"scheduler": {"config": {"inline": inline}}}},
    }
    path = tmp_path / "test.yaml"
    with open(path, "w") as f:
        yaml.dump(manifest, f)
    return path


class TestExtractRequiredPlugins:
    def test_extracts_types(self, tmp_path):
        path = _write_manifest(tmp_path, [{"type": "a"}, {"type": "b"}])
        assert extract_required_plugins(path) == {"a", "b"}

    def test_empty_when_no_plugins(self, tmp_path):
        path = tmp_path / "s.yaml"
        with open(path, "w") as f:
            yaml.dump({"apiVersion": "v1", "spec": {"model": {"name": "m"}}}, f)
        assert extract_required_plugins(path) == set()

    def test_optional_excluded(self, tmp_path):
        path = _write_manifest(tmp_path, [{"type": "req"}, {"type": "opt", "optional": True}])
        assert extract_required_plugins(path) == {"req"}

    def test_plugins_null_returns_empty(self, tmp_path):
        path = tmp_path / "null.yaml"
        with open(path, "w") as f:
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "spec": {"router": {"scheduler": {"config": {"inline": {"plugins": None}}}}},
                },
                f,
            )
        assert extract_required_plugins(path) == set()

    def test_scheduler_null_does_not_raise(self, tmp_path):
        path = tmp_path / "sched-null.yaml"
        with open(path, "w") as f:
            yaml.dump(
                {
                    "apiVersion": "v1",
                    "spec": {"router": {"scheduler": None}},
                },
                f,
            )
        assert extract_required_plugins(path) == set()

    def test_saturation_detector_ref(self, tmp_path):
        path = _write_manifest(
            tmp_path,
            [{"type": "a"}],
            extras={
                "flowControl": {"saturationDetector": {"pluginRef": "det"}},
            },
        )
        assert "det" in extract_required_plugins(path)


class TestCheckManifestCompatibility:
    def test_passes_when_all_available(self, tmp_path):
        path = _write_manifest(tmp_path, [{"type": "a"}])
        r = check_manifest_compatibility(path, frozenset({"a", "b"}), "test")
        assert r.compatible is True

    def test_fails_when_missing(self, tmp_path):
        path = _write_manifest(tmp_path, [{"type": "a"}, {"type": "new"}])
        r = check_manifest_compatibility(path, frozenset({"a"}), "test")
        assert r.compatible is False
        assert "new" in r.missing_plugins

    def test_no_plugins_always_passes(self, tmp_path):
        path = tmp_path / "s.yaml"
        with open(path, "w") as f:
            yaml.dump({"apiVersion": "v1", "spec": {"model": {"name": "m"}}}, f)
        assert check_manifest_compatibility(path, frozenset(), "test").compatible is True

    def test_none_available_skips(self, tmp_path):
        path = _write_manifest(tmp_path, [{"type": "a"}])
        r = check_manifest_compatibility(path, None)
        assert r.compatible is True
        assert "could not detect" in r.skipped_reason


class TestResolveAvailablePlugins:
    def test_falls_back_to_latest_when_cluster_tag_absent_locally(self, tmp_path, monkeypatch):
        """Cluster tag not in local repo tags → use latest (clone is incomplete)."""
        router = tmp_path / "router"
        router.mkdir()

        def kubectl(*_a, **_k):
            return ""

        monkeypatch.setattr(
            "conformance.preflight.probe_epp_plugins",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "conformance.preflight._detect_epp_version_tag",
            lambda *_a, **_k: "v9.9.9",
        )

        def fake_extract(_repo, tag: str):
            if tag == "v9.9.9":
                return frozenset()
            if tag == "v1.2.3":
                return frozenset({"plugin-a"})
            return frozenset()

        monkeypatch.setattr("conformance.preflight.extract_plugins_from_source", fake_extract)
        monkeypatch.setattr(
            "conformance.preflight._list_release_tags",
            lambda _repo: ["v1.0.0", "v1.2.3"],
        )

        plugins, source = resolve_available_plugins(kubectl, "ns", router_repo=router)
        assert plugins == frozenset({"plugin-a"})
        assert "v1.2.3" in source
        assert "v9.9.9" in source

    def test_skips_when_exact_tag_exists_but_extract_empty(self, tmp_path, monkeypatch):
        """Tag exists locally but extract empty → fail closed (do not use latest)."""
        router = tmp_path / "router"
        router.mkdir()

        monkeypatch.setattr(
            "conformance.preflight.probe_epp_plugins",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "conformance.preflight._detect_epp_version_tag",
            lambda *_a, **_k: "v1.2.3",
        )
        monkeypatch.setattr(
            "conformance.preflight.extract_plugins_from_source",
            lambda _repo, _tag: frozenset(),
        )
        monkeypatch.setattr(
            "conformance.preflight._list_release_tags",
            lambda _repo: ["v1.0.0", "v1.2.3"],
        )

        plugins, source = resolve_available_plugins(lambda *_a, **_k: "", "ns", router_repo=router)
        assert plugins is None
        assert source == "no detection method available"


class TestApiVersionCheck:
    def _make_manifest(self, tmp_path, api_version):
        path = tmp_path / "test.yaml"
        with open(path, "w") as f:
            yaml.dump(
                {
                    "apiVersion": api_version,
                    "kind": "LLMInferenceService",
                    "metadata": {"name": "test"},
                    "spec": {"model": {"name": "m"}},
                },
                f,
            )
        return path

    def test_served_version_passes(self, tmp_path):
        path = self._make_manifest(tmp_path, "serving.kserve.io/v1alpha2")

        def kubectl(*a, **k):
            return "v1alpha1 v1alpha2"

        r = check_api_version(path, kubectl)
        assert r.compatible is True

    def test_unserved_version_fails(self, tmp_path):
        path = self._make_manifest(tmp_path, "serving.kserve.io/v1alpha2")

        def kubectl(*a, **k):
            return "v1alpha1"

        r = check_api_version(path, kubectl)
        assert r.compatible is False
        assert "v1alpha2" in r.diagnosis

    def test_crd_not_found_skips(self, tmp_path):
        path = self._make_manifest(tmp_path, "serving.kserve.io/v1alpha2")

        def kubectl(*a, **k):
            return ""

        r = check_api_version(path, kubectl)
        assert r.compatible is True
        assert r.skipped_reason


class TestImageExistence:
    def test_skopeo_not_installed_skips(self):
        from unittest.mock import patch

        def kubectl(*a, **k):
            return "quay.io/rhoai/some-image@sha256:abc"

        with patch("subprocess.run", side_effect=FileNotFoundError):
            r = check_image_existence(kubectl)
        assert r.compatible is True
        assert "skopeo" in r.skipped_reason

    def test_no_epp_image_skips(self):
        def kubectl(*a, **k):
            return ""

        r = check_image_existence(kubectl)
        assert r.compatible is True
        assert "no EPP image" in r.skipped_reason

    def test_image_not_found_fails(self):
        from unittest.mock import patch
        import subprocess as sp

        def kubectl(*a, **k):
            return "quay.io/rhoai/fake-image:latest"

        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="manifest unknown",
            ),
        ):
            r = check_image_existence(kubectl)
        assert r.compatible is False
        assert "does not exist" in r.diagnosis

    def test_auth_error_skips_not_fails(self):
        from unittest.mock import patch
        import subprocess as sp

        def kubectl(*a, **k):
            return "quay.io/rhoai/private-image:latest"

        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="unauthorized: access denied",
            ),
        ):
            r = check_image_existence(kubectl)
        assert r.compatible is True
        assert "inconclusive" in r.skipped_reason

    def test_network_error_skips_not_fails(self):
        from unittest.mock import patch
        import subprocess as sp

        def kubectl(*a, **k):
            return "quay.io/rhoai/some-image:latest"

        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="connection refused",
            ),
        ):
            r = check_image_existence(kubectl)
        assert r.compatible is True
        assert "inconclusive" in r.skipped_reason

    def test_image_exists_passes(self):
        from unittest.mock import patch
        import subprocess as sp

        def kubectl(*a, **k):
            return "quay.io/rhoai/real-image@sha256:abc"

        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess(
                args=[],
                returncode=0,
                stdout="{}",
                stderr="",
            ),
        ):
            r = check_image_existence(kubectl)
        assert r.compatible is True


class TestProbeEppPlugins:
    def test_ignores_handler_names_on_registered_line(self):
        logs = (
            'Registered plugin "flow-control-dispatcher" using handler "round-robin"\n'
            'plugin type "prefix-cache-scorer" ready\n'
        )

        def kubectl(*args, **_k):
            if args and args[0] == "get":
                return "epp-0"
            if args and args[0] == "logs":
                return logs
            return ""

        plugins = probe_epp_plugins(kubectl, "ns")
        assert plugins == frozenset({"flow-control-dispatcher", "prefix-cache-scorer"})
        assert "round-robin" not in plugins


class TestSourceExtractParsing:
    def test_parse_ignores_unrelated_type_assignments(self, tmp_path, monkeypatch):
        """Bare Type = \"json\" style lines must not become available plugins."""
        router = tmp_path / "router"
        router.mkdir()

        import subprocess as sp

        def fake_run(cmd, **kwargs):
            # Only PluginType greps return plugin lines; no broad Type= dump.
            pattern = cmd[3] if len(cmd) > 3 else ""
            if "PluginType" in pattern:
                stdout = 'const PluginType = "prefix-cache-scorer"\nPluginType = "token-producer"\n'
                return sp.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")
            return sp.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        monkeypatch.setattr("conformance.preflight.subprocess.run", fake_run)
        plugins = extract_plugins_from_source(router, "v1.0.0")
        assert plugins == frozenset({"prefix-cache-scorer", "token-producer"})


class TestSourceExtract:
    def test_extracts_from_router(self):
        router = Path.home() / "redhat" / "llm-d-router"
        if not router.exists():
            import pytest

            pytest.skip("router repo not cloned")
        import subprocess

        tags = [
            t.strip()
            for t in subprocess.run(
                ["git", "tag", "--list", "v*.*.*", "--sort=v:refname"],
                capture_output=True,
                text=True,
                cwd=str(router),
            ).stdout.splitlines()
            if t.strip() and "rc" not in t
        ]
        if not tags:
            import pytest

            pytest.skip("no tags")
        plugins = extract_plugins_from_source(router, tags[-1])
        assert len(plugins) > 0
        # Broad Type= junk from CRD/condition enums must not leak in.
        assert not {"exact", "accepted", "console"} & plugins

    def test_two_tags_both_extractable(self):
        """Extract on two tags without asserting older_count < newer_count.

        Plugin removals/renames are legitimate; only require newest to work and
        that an older tag either yields plugins or is skippable (pre-PluginType era).
        """
        router = Path.home() / "redhat" / "llm-d-router"
        if not router.exists():
            import pytest

            pytest.skip("router repo not cloned")
        import subprocess

        tags = [
            t.strip()
            for t in subprocess.run(
                ["git", "tag", "--list", "v*.*.*", "--sort=v:refname"],
                capture_output=True,
                text=True,
                cwd=str(router),
            ).stdout.splitlines()
            if t.strip() and "rc" not in t
        ]
        if len(tags) < 2:
            import pytest

            pytest.skip("need 2+ tags")
        newest = extract_plugins_from_source(router, tags[-1])
        assert len(newest) > 0
        oldest = extract_plugins_from_source(router, tags[0])
        # Older tags may predate PluginType constants — empty is OK, crash is not.
        assert isinstance(oldest, frozenset)
