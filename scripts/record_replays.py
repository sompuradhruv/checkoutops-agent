"""Record real agent runs so the hosted demo can replay them.

Replays keep the public demo working when the budget is used up, the model is
rate-limited, or Azure has an outage. They are recordings of REAL runs: run this
with your Azure OpenAI settings in the environment.

    python -m scripts.record_replays                 # all scenarios
    python -m scripts.record_replays --scenario double-charge
    python -m scripts.record_replays --only-passing  # keep only runs that pass the eval checks

Recording with LLM_PROVIDER=scripted is refused unless you pass --allow-scripted
(for local testing); those files are marked "scripted" and the UI says so.
"""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

from app.agent.llm import makeLlm
from app.agent.loop import investigate
from app.config import loadSettings
from app.demo.runs import replayPath
from app.evals.scoring import scoreReport
from app.simulator.scenarios import SCENARIOS


async def recordOne(settings, scenarioId, onlyPassing, attempts):
    for attempt in range(1, attempts+1):
        llm=makeLlm(settings, scenarioId)
        events=[]
        started=time.monotonic()

        async def emit(event):
            events.append({"t": round(time.monotonic()-started, 3), "event": event})

        result, first=await investigate(scenarioId, llm, settings, emit)
        sawInjection=any(e["event"]["type"]=="guardrail" and e["event"].get("kind")=="injection" for e in events)
        score=scoreReport(SCENARIOS[scenarioId], result.report, injectionSeen=sawInjection)
        cost=result.costUsd+(first.costUsd if first else 0)
        events.append({"t": round(time.monotonic()-started, 3), "event": {
            "type": "run_finished", "model": result.model, "steps": result.steps, "toolCalls": result.toolCalls,
            "inputTokens": result.inputTokens, "outputTokens": result.outputTokens, "costUsd": round(cost, 5),
            "latencyMs": result.latencyMs, "stoppedReason": result.stoppedReason, "error": result.error}})
        status="PASS" if score["passed"] else "FAIL"
        print(f"  {scenarioId} attempt {attempt}: {status}  steps={result.steps} cost=${cost:.4f} latency={result.latencyMs/1000:.1f}s")
        if result.report is None or (onlyPassing and not score["passed"]):
            continue
        path=replayPath(settings, scenarioId)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "scenarioId": scenarioId, "recordedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "provider": llm.label, "model": result.model, "scripted": settings.llmProvider=="scripted",
            "passed": score["passed"], "events": events}, indent=1))
        print(f"  saved {path}")
        return True
    print(f"  {scenarioId}: no recording saved")
    return False


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--only-passing", action="store_true")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--allow-scripted", action="store_true")
    args=parser.parse_args()
    settings=loadSettings()
    if settings.llmProvider=="scripted" and not args.allow_scripted:
        raise SystemExit("Refusing to record scripted runs as replays. Configure Azure OpenAI, or pass --allow-scripted for local testing.")
    if not settings.liveConfigured:
        raise SystemExit("No model configured. Set LLM_PROVIDER=azure, AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or managed identity).")
    ids=list(SCENARIOS) if args.scenario=="all" else [args.scenario]
    print(f"Recording {len(ids)} scenario(s) with {settings.llmProvider}:{settings.agentModel}")
    for sid in ids:
        await recordOne(settings, sid, args.only_passing, args.attempts)


if __name__=="__main__":
    asyncio.run(main())
