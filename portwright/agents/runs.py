"""Run history: one JSON trace per agent/workflow run under ``.portwright/agent_runs``."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

RUNS_DIR = ".portwright/agent_runs"


def new_run_id() -> str:
    now = time.time()      # milliseconds, so runs started in the same second still sort in start order
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"{int(now * 1000) % 1000:03d}-" + uuid.uuid4().hex[:4]


def save_run(root: str | Path, kind: str, name: str, run_id: str, data: dict) -> Path:
    d = Path(root) / RUNS_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{run_id}_{kind}_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.json"
    p.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return p


def list_runs(root: str | Path, limit: int = 20) -> list[dict]:
    d = Path(root) / RUNS_DIR
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:limit]:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"run_id": j.get("run_id"), "kind": j.get("kind"), "name": j.get("name"),
                    "stopped_by": j.get("stopped_by"), "tokens": j.get("tokens"), "file": str(p)})
    return out
