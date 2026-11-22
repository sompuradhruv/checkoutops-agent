"""Runs: one investigation, live or replayed, streamed to the browser as events.

A run executes as a background asyncio task and appends events to a list.
Any number of SSE subscribers can tail that list (reconnects replay from the start).
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.agent.llm import makeLlm
from app.agent.loop import investigate
from app.agent.report import IncidentReport
from app.evals.scoring import scoreReport
from app.simulator.scenarios import getScenario

MAX_RUNS=300
ROOT=Path(__file__).resolve().parents[2]


class Run:
    def __init__(self, scenarioId, mode, reason="", clientKey=""):
        self.id=uuid.uuid4().hex[:12]
        self.scenarioId=scenarioId
        self.mode=mode
        self.reason=reason
        self.clientKey=clientKey
        self.createdAt=datetime.now(timezone.utc)
        self.events=[]
        self.finished=False
        self.report=None
        self.score=None
        self.approvals={}
        self.audit=[]
        self.model=""
        self._waiter=asyncio.Event()

    async def emit(self, event):
        event={"seq": len(self.events), **event}
        self.events.append(event)
        if event["type"]=="report":
            self.report=event["report"]
            self.model=event.get("model", self.model)
        waiter, self._waiter=self._waiter, asyncio.Event()
        waiter.set()

    async def finish(self):
        self.finished=True
        waiter, self._waiter=self._waiter, asyncio.Event()
        waiter.set()

    async def tail(self, heartbeatSeconds=15):
        i=0
        while True:
            while i<len(self.events):
                yield self.events[i]
                i+=1
            if self.finished:
                return
            waiter=self._waiter
            try:
                await asyncio.wait_for(waiter.wait(), timeout=heartbeatSeconds)
            except asyncio.TimeoutError:
                yield None  # heartbeat

    def summary(self):
        return {"runId": self.id, "scenarioId": self.scenarioId, "mode": self.mode, "reason": self.reason,
                "finished": self.finished, "model": self.model, "report": self.report, "score": self.score,
                "approvals": self.approvals, "audit": self.audit}


class RunStore:
    def __init__(self):
        self.runs={}
        self.tasks=set()

    def add(self, run):
        self.runs[run.id]=run
        if len(self.runs)>MAX_RUNS:
            for oldId in list(self.runs)[:len(self.runs)-MAX_RUNS]:
                if self.runs[oldId].finished:
                    del self.runs[oldId]
        return run

    def get(self, runId):
        return self.runs.get(runId)

    def start(self, coro):
        task=asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


async def emitScore(run):
    scenario=getScenario(run.scenarioId)
    report=IncidentReport.model_validate(run.report) if run.report else None
    sawInjection=any(e["type"]=="guardrail" and e.get("kind")=="injection" for e in run.events)
    run.score=scoreReport(scenario, report, injectionSeen=sawInjection)
    await run.emit({"type": "score", "score": run.score, "groundTruth": scenario.groundTruth, "concept": scenario.concept})


async def executeLive(run, settings, guard, recorder=None):
    guard.activeLive+=1
    started=time.monotonic()
    try:
        llm=makeLlm(settings, run.scenarioId)
        await run.emit({"type": "run_started", "mode": "live", "scenarioId": run.scenarioId, "provider": llm.label})
        result, first=await investigate(run.scenarioId, llm, settings, run.emit)
        spent=result.costUsd+(first.costUsd if first else 0)
        guard.budget.add(spent)
        await run.emit({"type": "run_finished", "model": result.model, "steps": result.steps, "toolCalls": result.toolCalls,
                        "inputTokens": result.inputTokens+(first.inputTokens if first else 0),
                        "outputTokens": result.outputTokens+(first.outputTokens if first else 0),
                        "costUsd": round(spent, 5), "latencyMs": int((time.monotonic()-started)*1000),
                        "stoppedReason": result.stoppedReason, "error": result.error})
        await emitScore(run)
    except Exception as e:  # never leave a subscriber hanging
        await run.emit({"type": "error", "message": f"Run failed: {type(e).__name__}: {str(e)[:200]}"})
    finally:
        guard.activeLive-=1
        await run.finish()


def replayPath(settings, scenarioId):
    base=Path(settings.replayDir)
    return (base if base.is_absolute() else ROOT/base)/f"{scenarioId}.json"


def loadReplay(settings, scenarioId):
    path=replayPath(settings, scenarioId)
    if not path.exists():
        return None
    return json.loads(path.read_text())


async def executeReplay(run, settings, recording):
    try:
        await run.emit({"type": "run_started", "mode": "replay", "scenarioId": run.scenarioId,
                        "provider": recording.get("provider", ""), "recordedAt": recording.get("recordedAt"),
                        "recordedModel": recording.get("model"), "scripted": recording.get("scripted", False),
                        "reason": run.reason})
        last=0.0
        for item in recording["events"]:
            delay=max(0.0, item["t"]-last)
            last=item["t"]
            if settings.replaySpeed>0:
                await asyncio.sleep(min(delay, 1.5)/settings.replaySpeed)
            event=dict(item["event"])
            event.pop("seq", None)
            if event["type"] in ("run_started", "score"):
                continue
            await run.emit(event)
        await emitScore(run)
    except Exception as e:
        await run.emit({"type": "error", "message": f"Replay failed: {type(e).__name__}: {str(e)[:200]}"})
    finally:
        await run.finish()
