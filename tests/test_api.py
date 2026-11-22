import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent.llm import ScriptedLlm
from app.agent.loop import investigate
from app.demo.protection import DemoGuard
from app.demo.runs import RunStore, replayPath


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "guard", DemoGuard(settings))
    monkeypatch.setattr(main, "store", RunStore())
    with TestClient(main.app) as c:
        yield c


def stream(client, runId):
    events=[]
    with client.stream("GET", f"/api/runs/{runId}/events") as s:
        for line in s.iter_lines():
            if line.startswith("data: ") and line!="data: {}":
                events.append(json.loads(line[6:]))
    return events


def writeReplay(settings, sid):
    recorded=[]

    async def emit(e):
        recorded.append({"t": len(recorded)*0.01, "event": e})

    asyncio.run(investigate(sid, ScriptedLlm(sid), settings, emit))
    path=replayPath(settings, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scenarioId": sid, "recordedAt": "2026-11-22T10:00Z", "provider": "test", "model": "test",
                                "scripted": True, "events": recorded}))


def test_live_run_streams_report_and_score(client):
    run=client.post("/api/runs", json={"scenarioId": "double-charge"}).json()
    assert run["mode"]=="live"
    events=stream(client, run["runId"])
    types=[e["type"] for e in events]
    assert types[0]=="run_started" and "tool_call" in types and "report" in types and types[-1]=="score"
    assert events[-1]["score"]["passed"]
    summary=client.get(f"/api/runs/{run['runId']}").json()
    assert summary["finished"] and summary["report"]["rootCause"]["category"]=="idempotency_bug"


def test_approval_simulates_the_action_once(client):
    run=client.post("/api/runs", json={"scenarioId": "latency-bad-deploy"}).json()
    stream(client, run["runId"])
    first=client.post(f"/api/runs/{run['runId']}/actions/0/approve").json()
    assert first["resolved"] and "Alert resolved" in first["outcome"]
    again=client.post(f"/api/runs/{run['runId']}/actions/0/approve").json()
    assert again==first
    assert client.post(f"/api/runs/{run['runId']}/actions/9/approve").status_code==400
    assert len(client.get(f"/api/runs/{run['runId']}").json()["audit"])==1


def test_no_model_and_no_recording_is_a_clear_error(client, settings):
    settings.llmProvider="none"
    res=client.post("/api/runs", json={"scenarioId": "double-charge"})
    assert res.status_code==429 and "No model" in res.json()["detail"]


def test_falls_back_to_recording_when_live_is_unavailable(client, settings):
    writeReplay(settings, "double-charge")
    settings.llmProvider="none"
    run=client.post("/api/runs", json={"scenarioId": "double-charge"}).json()
    assert run["mode"]=="replay" and "No model" in run["reason"]
    events=stream(client, run["runId"])
    assert events[0]["type"]=="run_started" and events[0]["mode"]=="replay" and events[0]["scripted"]
    assert events[-1]["type"]=="score" and events[-1]["score"]["passed"]


def test_rate_limit_uses_the_proxy_appended_ip(client, settings):
    settings.liveRunsPerIpPerHour=1
    main.guard=DemoGuard(settings)
    ok=client.post("/api/runs", json={"scenarioId": "double-charge"}, headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
    assert ok.json()["mode"]=="live"
    spoofed=client.post("/api/runs", json={"scenarioId": "double-charge"}, headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
    assert spoofed.status_code==429
    other=client.post("/api/runs", json={"scenarioId": "double-charge"}, headers={"X-Forwarded-For": "8.8.8.8"})
    assert other.json()["mode"]=="live"


def test_budget_exhaustion_switches_to_recordings(client, settings):
    writeReplay(settings, "latency-bad-deploy")
    main.guard.budget.add(settings.dailyBudgetUsd+1)
    run=client.post("/api/runs", json={"scenarioId": "latency-bad-deploy"}).json()
    assert run["mode"]=="replay" and "budget" in run["reason"]


def test_overview_and_scenarios_hide_ground_truth(client):
    scenarios=client.get("/api/scenarios").json()
    assert len(scenarios)==6
    text=json.dumps(scenarios)+json.dumps(client.get("/api/scenarios/prompt-injection/overview").json())
    assert "d-4840" not in text and "groundTruth" not in text


def test_security_headers_and_static_page(client):
    res=client.get("/")
    assert res.status_code==200 and "CheckoutOps Agent" in res.text
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"]=="DENY"


def test_unknown_things_404(client):
    assert client.get("/api/scenarios/nope/overview").status_code==404
    assert client.post("/api/runs", json={"scenarioId": "nope"}).status_code==404
    assert client.get("/api/runs/nope/events").status_code==404
    assert client.post("/api/runs", json={"scenarioId": "double-charge", "mode": "replay"}).status_code==404
