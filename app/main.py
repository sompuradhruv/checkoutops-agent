"""HTTP API and static UI.

POST /api/runs starts an investigation (live or replay) and returns a run id.
GET  /api/runs/{id}/events streams the agent's steps as Server-Sent Events.
POST /api/runs/{id}/actions/{i}/approve is the human-in-the-loop gate.
"""
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.prompts import PROMPT_VERSION
from app.agent.report import ACTION_LABELS
from app.config import loadSettings
from app.demo.protection import DemoGuard
from app.demo.runs import Run, RunStore, executeLive, executeReplay, loadReplay
from app.simulator.platform import fmtTime
from app.simulator.scenarios import SCENARIOS

ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/"static"

settings=loadSettings()
guard=DemoGuard(settings)
store=RunStore()

app=FastAPI(title="CheckoutOps Agent", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

CSP=("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
     "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def securityHeaders(request, callNext):
    response=await callNext(request)
    if not request.url.path.startswith("/api/docs"):
        response.headers["Content-Security-Policy"]=CSP
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Referrer-Policy"]="no-referrer"
    response.headers["X-Frame-Options"]="DENY"
    return response


def clientKey(request: Request):
    # Behind Azure Container Apps the ingress appends the real client IP as the LAST
    # X-Forwarded-For entry. Earlier entries are client-supplied and can be spoofed.
    forwarded=request.headers.get("x-forwarded-for", "")
    if forwarded:
        return stripPort(forwarded.split(",")[-1].strip())
    return request.client.host if request.client else "unknown"


def scenarioOr404(scenarioId):
    if scenarioId not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario '{scenarioId}'")
    return SCENARIOS[scenarioId]


def stripPort(address):
    # Azure App Service forwards "1.2.3.4:51234"; the port changes per connection, the IP does not.
    if address.startswith("["):
        return address[1:address.find("]")] if "]" in address else address
    if address.count(":")==1:
        return address.split(":")[0]
    return address


@app.get("/")
async def index():
    return FileResponse(STATIC/"index.html")


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def config():
    providerLabel={"azure": "Azure OpenAI", "openai": "OpenAI", "scripted": "Scripted (no LLM)"}.get(settings.llmProvider, "none")
    return {"provider": providerLabel, "liveAvailable": settings.liveConfigured and guard.budget.canSpend(),
            "liveConfigured": settings.liveConfigured, "model": settings.agentModel, "escalationModel": settings.escalationModel or None,
            "promptVersion": PROMPT_VERSION, "budget": guard.budget.status(),
            "limits": {"liveRunsPerHour": settings.liveRunsPerIpPerHour, "maxSteps": settings.maxSteps},
            "replays": [sid for sid in SCENARIOS if loadReplay(settings, sid)], "actionLabels": ACTION_LABELS}


@app.get("/api/scenarios")
async def scenarios():
    out=[]
    for s in SCENARIOS.values():
        alert=s.telemetry().alert
        out.append({"id": s.id, "title": s.title, "teaser": s.teaser, "hasReplay": loadReplay(settings, s.id) is not None,
                    "alert": {"title": alert.title, "service": alert.service, "metric": alert.metric, "firedAt": fmtTime(alert.firedIndex)}})
    return out


@app.get("/api/scenarios/{scenarioId}/overview")
async def overview(scenarioId: str):
    s=scenarioOr404(scenarioId)
    t=s.telemetry()
    a=t.alert
    values=t.series(a.service, a.metric)
    return {"id": s.id, "title": s.title, "teaser": s.teaser,
            "alert": {"title": a.title, "service": a.service, "metric": a.metric, "threshold": a.threshold,
                      "comparison": a.comparison, "unit": a.unit, "firedAt": fmtTime(a.firedIndex), "firedIndex": a.firedIndex},
            "series": [{"t": fmtTime(i), "v": round(v, 4)} for i, v in enumerate(values)]}


class RunRequest(BaseModel):
    scenarioId: str
    mode: Literal["auto", "live", "replay"]="auto"


@app.post("/api/runs")
async def createRun(body: RunRequest, request: Request):
    scenarioOr404(body.scenarioId)
    recording=loadReplay(settings, body.scenarioId)
    key=clientKey(request)
    if body.mode=="replay":
        if not recording:
            raise HTTPException(404, "No recording exists for this scenario yet.")
        run=store.add(Run(body.scenarioId, "replay", "You chose a recorded run.", key))
        store.start(executeReplay(run, settings, recording))
        return {"runId": run.id, "mode": "replay", "reason": run.reason}
    allowed, reason=guard.admitLive(key)
    if allowed:
        run=store.add(Run(body.scenarioId, "live", "", key))
        store.start(executeLive(run, settings, guard))
        return {"runId": run.id, "mode": "live", "reason": ""}
    if recording:
        run=store.add(Run(body.scenarioId, "replay", reason, key))
        store.start(executeReplay(run, settings, recording))
        return {"runId": run.id, "mode": "replay", "reason": reason}
    raise HTTPException(429, reason+" No recording exists for this scenario yet.")


@app.get("/api/runs/{runId}/events")
async def runEvents(runId: str):
    run=store.get(runId)
    if not run:
        raise HTTPException(404, "Unknown run")

    async def stream():
        async for event in run.tail():
            if event is None:
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{runId}")
async def runSummary(runId: str):
    run=store.get(runId)
    if not run:
        raise HTTPException(404, "Unknown run")
    return run.summary()


@app.post("/api/runs/{runId}/actions/{index}/approve")
async def approveAction(runId: str, index: int, request: Request):
    run=store.get(runId)
    if not run:
        raise HTTPException(404, "Unknown run")
    if not run.finished or not run.report:
        raise HTTPException(409, "The run has not produced a report yet.")
    actions=run.report["recommendedActions"]
    if index<0 or index>=len(actions):
        raise HTTPException(400, "No such action")
    if index in run.approvals:
        return run.approvals[index]
    action=actions[index]
    outcome=SCENARIOS[run.scenarioId].simulateAction(action["action"], action["target"])
    record={"index": index, "action": action["action"], "target": action["target"], **outcome}
    run.approvals[index]=record
    run.audit.append({"event": "action_approved", "index": index, "action": action["action"], "target": action["target"],
                      "approvedBy": "demo visitor", "resolved": outcome["resolved"]})
    return record


@app.get("/api/evals/latest")
async def latestEvals():
    path=ROOT/settings.evalResultsDir/"latest.json"
    if not path.exists():
        return JSONResponse({"available": False})
    data=json.loads(path.read_text())
    data["available"]=True
    return data
