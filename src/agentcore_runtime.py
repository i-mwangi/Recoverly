from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

if "src" not in sys.modules:
    package = types.ModuleType("src")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules["src"] = package

from src.agents.recoverly_agent import run_cycle

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke_recoverly(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "status": "invalid_payload"}
    reference_date = payload.get("reference_date", "")
    if not isinstance(reference_date, str):
        return {"ok": False, "status": "invalid_reference_date"}
    return run_cycle(reference_date=reference_date)


if __name__ == "__main__":
    app.run()
