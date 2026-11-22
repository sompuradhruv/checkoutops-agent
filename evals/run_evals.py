"""Eval suite: run every scenario N times per model and score the reports.

    python -m evals.run_evals                                  # AGENT_MODEL, 3 repeats
    python -m evals.run_evals --models gpt-5-mini,gpt-5 --repeats 3
    LLM_PROVIDER=scripted python -m evals.run_evals --repeats 1 # plumbing check, no LLM

Writes evals/results/latest.json (shown on the demo's Evals tab) and a
timestamped copy. Runs are sequential to stay under rate limits.
Exit code 1 if the pass rate is below --min-pass-rate (useful as a CI gate when
prompts or models change).
"""
import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.agent.llm import makeLlm
from app.agent.loop import investigate
from app.agent.prompts import PROMPT_VERSION
from app.config import loadSettings
from app.evals.scoring import scoreReport
from app.simulator.scenarios import SCENARIOS

ROOT=Path(__file__).resolve().parents[1]


async def runOnce(settings, scenarioId, model):
    llm=makeLlm(settings, scenarioId)
    guardrails=[]

    async def emit(event):
        if event["type"]=="guardrail":
            guardrails.append(event["kind"])

    started=time.monotonic()
    result, _=await investigate(scenarioId, llm, settings, emit, modelOverride=model, allowEscalation=False)
    score=scoreReport(SCENARIOS[scenarioId], result.report, injectionSeen="injection" in guardrails)
    return {"scenarioId": scenarioId, "model": model, "passed": score["passed"], "checks": score["checks"],
            "category": result.report.rootCause.category if result.report else None,
            "confidence": result.report.confidence if result.report else None,
            "steps": result.steps, "toolCalls": result.toolCalls, "inputTokens": result.inputTokens,
            "outputTokens": result.outputTokens, "costUsd": round(result.costUsd, 5),
            "latencyS": round(time.monotonic()-started, 2), "error": result.error, "guardrails": guardrails}


def summarise(runs, models, scenarioIds):
    summary={}
    for m in models:
        rows=[r for r in runs if r["model"]==m]
        if not rows:
            continue
        summary[m]={
            "passRate": round(sum(r["passed"] for r in rows)/len(rows), 3), "runs": len(rows),
            "avgCostUsd": round(statistics.mean(r["costUsd"] for r in rows), 5),
            "avgLatencyS": round(statistics.mean(r["latencyS"] for r in rows), 2),
            "avgSteps": round(statistics.mean(r["steps"] for r in rows), 1),
            "perScenario": {sid: round(sum(r["passed"] for r in rows if r["scenarioId"]==sid)/max(1, sum(1 for r in rows if r["scenarioId"]==sid)), 3)
                            for sid in scenarioIds},
        }
    return summary


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--models", default="")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--min-pass-rate", type=float, default=0.0)
    parser.add_argument("--out", default="")
    args=parser.parse_args()
    settings=loadSettings()
    if not settings.liveConfigured:
        raise SystemExit("No model configured (set LLM_PROVIDER etc.), or use LLM_PROVIDER=scripted for a plumbing check.")
    models=[m.strip() for m in args.models.split(",") if m.strip()] or [settings.agentModel]
    if settings.llmProvider=="scripted":
        models=["scripted"]
    scenarioIds=list(SCENARIOS) if args.scenario=="all" else [args.scenario]
    runs=[]
    for model in models:
        for sid in scenarioIds:
            for i in range(args.repeats):
                r=await runOnce(settings, sid, model)
                runs.append(r)
                failed=[c["name"] for c in r["checks"] if not c["passed"]]
                print(f"{model:>14} {sid:<26} #{i+1} {'PASS' if r['passed'] else 'FAIL'} steps={r['steps']:>2} "
                      f"${r['costUsd']:.4f} {r['latencyS']:>5.1f}s {('failed: '+', '.join(failed)) if failed else ''}")
    summary=summarise(runs, models, scenarioIds)
    out={"generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "provider": settings.llmProvider,
         "promptVersion": PROMPT_VERSION, "repeats": args.repeats, "models": models, "scenarios": scenarioIds,
         "summary": summary, "runs": runs}
    resultsDir=ROOT/settings.evalResultsDir
    resultsDir.mkdir(parents=True, exist_ok=True)
    target=Path(args.out) if args.out else resultsDir/"latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=1))
    if not args.out:
        (resultsDir/f"run-{out['generatedAt'].replace(':', '')}.json").write_text(json.dumps(out, indent=1))
    print("\nModel            pass rate  avg cost   avg latency  avg steps")
    for m, s in summary.items():
        print(f"{m:<16} {s['passRate']*100:>8.0f}%  ${s['avgCostUsd']:.4f}   {s['avgLatencyS']:>8.1f}s  {s['avgSteps']:>8}")
    print(f"\nSaved {target}")
    worst=min((s["passRate"] for s in summary.values()), default=0)
    if worst<args.min_pass_rate:
        raise SystemExit(f"Pass rate {worst:.0%} is below the required {args.min_pass_rate:.0%}")


if __name__=="__main__":
    asyncio.run(main())
