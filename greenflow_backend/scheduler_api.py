"""
scheduler_api.py - the scheduler as a small web service (FastAPI).

Install and run:
    pip install fastapi uvicorn
    python scheduler_api.py            # then open http://127.0.0.1:8000/docs

Endpoints:
    GET  /health      -> {"status": "ok"}
    GET  /workflows   -> the built-in workflows
    GET  /example     -> an example request (add ?workflow=research or support)
    POST /schedule    -> send a request, get a plan and a receipt for every step
    POST /ingest      -> record completed-plan telemetry from a client (idempotent, for offline sync)
    GET  /fleet       -> reconciled fleet-wide totals from everything ever ingested

All results are SIMULATED (synthetic carbon curves and assumed energy/accuracy figures)
unless the request supplies real carbonProfiles and overrides.

/ingest and /fleet exist to demonstrate offline resilience: the browser client queues run
records locally (localStorage) while this service is unreachable, then flushes the queue here
in one batch once connectivity returns. /ingest is idempotent by client-generated id, so a
retried flush after a partial failure never double-counts. Nothing about /schedule depends on
this — the scheduler itself has no network dependency at all.
"""
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import Body, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import scheduler_core as core

app = FastAPI(
    title="Carbon-aware workflow scheduler",
    description="Chooses a model size, region and start time for each step of an AI agent workflow. Simulated results.",
    version="1.0",
)

# Open CORS so a demo web page can call the service. Restrict this before any real deployment.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- fleet telemetry (offline-sync demonstration) ----------
_FLEET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_log.json")
_lock = threading.Lock()


def _load_fleet() -> Dict[str, Dict[str, Any]]:
    if os.path.exists(_FLEET_PATH):
        try:
            with open(_FLEET_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_fleet(records: Dict[str, Dict[str, Any]]) -> None:
    tmp = _FLEET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f)
    os.replace(tmp, _FLEET_PATH)


_fleet: Dict[str, Dict[str, Any]] = _load_fleet()


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/workflows")
def workflows() -> Dict[str, Any]:
    return {k: {"name": w["name"], "deadline_hours": w["deadline"], "steps": [s["id"] + " " + s["name"] for s in w["steps"]]}
            for k, w in core.WORKFLOWS.items()}


@app.get("/example")
def example(workflow: str = Query("docs", description="docs, research or support")) -> Dict[str, Any]:
    if workflow not in core.WORKFLOWS:
        return JSONResponse(status_code=404, content={"ok": False, "errors": ["Unknown workflow. Use: " + ", ".join(core.WORKFLOWS)]})
    return core.example_spec(workflow)


@app.post("/schedule")
def schedule(spec: Dict[str, Any] = Body(..., description="A workflow request; see /example for the format")):
    result = core.schedule(spec)
    if not result["ok"]:
        return JSONResponse(status_code=422, content=result)
    return result


@app.post("/ingest")
def ingest(body: Dict[str, Any] = Body(..., description='{"records": [{"id": "...", "workflow": "...", "logged_at": "...", "summary": {...}}]}')):
    """Accept a batch of run-telemetry records queued by a client while it was offline.
    Idempotent by record id: re-sending an already-stored id is a no-op, so a client that
    retries a partially-failed flush never double-counts a run."""
    records = body.get("records")
    if not isinstance(records, list) or not records:
        return JSONResponse(status_code=422, content={"ok": False, "errors": ["Expected a non-empty 'records' list."]})
    stored, already_had, rejected = [], [], []
    with _lock:
        for rec in records:
            rid = rec.get("id") if isinstance(rec, dict) else None
            if not rid or not isinstance(rid, str):
                rejected.append(rec)
                continue
            if rid in _fleet:
                already_had.append(rid)
                continue
            _fleet[rid] = {
                "id": rid,
                "workflow": rec.get("workflow", "unknown"),
                "logged_at": rec.get("logged_at"),
                "received_at": datetime.now(timezone.utc).isoformat(),
                "summary": rec.get("summary", {}),
            }
            stored.append(rid)
        if stored:
            _save_fleet(_fleet)
    return {"ok": True, "stored_ids": stored, "already_had": already_had, "rejected": len(rejected),
            "fleet_totals": _totals()}


@app.get("/fleet")
def fleet() -> Dict[str, Any]:
    """Reconciled fleet-wide totals across everything ever ingested (server's source of truth)."""
    return {"ok": True, "run_count": len(_fleet), "totals": _totals(),
            "recent": sorted(_fleet.values(), key=lambda r: r.get("received_at", ""), reverse=True)[:10]}


def _totals() -> Dict[str, float]:
    carbon_g = cost_usd = energy_kwh = 0.0
    for rec in _fleet.values():
        s = rec.get("summary") or {}
        carbon_g += float(s.get("carbon_g") or 0)
        cost_usd += float(s.get("cost_usd") or 0)
        energy_kwh += float(s.get("energy_kwh") or 0)
    return {"carbon_g": round(carbon_g, 3), "cost_usd": round(cost_usd, 4), "energy_kwh": round(energy_kwh, 4)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
