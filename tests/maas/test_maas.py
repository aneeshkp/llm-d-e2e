from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from conformance.maas import MaaSResource, wait_for_maas_ready

_MODEL_REF = MaaSResource("MaaSModelRef", "maas-single-gpu", "llm-conformance-test")
_POLICY = MaaSResource("MaaSAuthPolicy", "maas-single-gpu-auth", "models-as-a-service")


def _kubectl_returning(phases: dict[str, list[str]]):
    """Fake kubectl: each ``get <kind>`` returns the next phase queued for that kind (last one repeats)."""

    def kubectl(*args):
        queue = phases[args[1]]
        phase = queue.pop(0) if len(queue) > 1 else queue[0]
        status = {"phase": phase, "endpoint": "https://203.0.113.10/", "resolvedModelAlias": "publishers/x/models/q"}
        return json.dumps({"status": status})

    return kubectl


def test_waits_through_pending_and_returns_model_ref_status():
    kubectl = _kubectl_returning({"maasmodelref": ["Pending", "Pending", "Ready"], "maasauthpolicy": ["Active"]})

    status = wait_for_maas_ready(kubectl, [_MODEL_REF, _POLICY], timeout=5, poll_interval=0)

    assert status["endpoint"] == "https://203.0.113.10/"


def test_transient_failed_phase_is_tolerated():
    kubectl = _kubectl_returning({"maasmodelref": ["Failed", "Failed", "Ready"]})

    assert wait_for_maas_ready(kubectl, [_MODEL_REF], timeout=5, poll_interval=0)["phase"] == "Ready"


def test_persistent_failed_phase_fails_fast():
    kubectl = _kubectl_returning({"maasmodelref": ["Failed"]})

    with pytest.raises(RuntimeError, match="MaaSModelRef/maas-single-gpu is Failed"):
        wait_for_maas_ready(kubectl, [_MODEL_REF], timeout=60, poll_interval=0)
