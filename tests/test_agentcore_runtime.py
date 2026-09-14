from __future__ import annotations

from src.agentcore_runtime import invoke_recoverly


def test_agentcore_entrypoint_runs_the_autonomous_cycle(monkeypatch):
    monkeypatch.setattr(
        "src.agentcore_runtime.run_cycle",
        lambda reference_date: {"ok": True, "reference_date": reference_date},
    )

    result = invoke_recoverly({"reference_date": "2026-09-14"}, object())

    assert result == {"ok": True, "reference_date": "2026-09-14"}


def test_agentcore_entrypoint_rejects_invalid_payload():
    assert invoke_recoverly([], object()) == {"ok": False, "status": "invalid_payload"}
