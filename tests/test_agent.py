import copy
import json

from app.agent.llm import LlmError, LlmResponse, ScriptedLlm
from app.agent.loop import investigate, runAgent
from app.evals.scoring import scoreReport
from app.simulator.scenarios import SCENARIOS
from tests.conftest import SequenceLlm, collector, toolResponse


async def test_scripted_runs_pass_every_scenario(settings):
    for sid, sc in SCENARIOS.items():
        events, emit=collector()
        result, _=await investigate(sid, ScriptedLlm(sid), settings, emit)
        assert result.report is not None, sid
        assert scoreReport(sc, result.report)["passed"], sid
        assert events[-1]["type"]=="report"


async def test_secrets_never_reach_the_model(settings):
    llm=SequenceLlm([toolResponse("getServiceConfig", {"service": "payment-service"}, "c1"),
                     toolResponse("searchLogs", {"query": "authorization bearer", "service": "payment-service", "minutes": 120}, "c2"),
                     toolResponse("submitReport", SCENARIOS["expired-credential"].scriptedReport, "c3")])
    events, emit=collector()
    result=await runAgent("expired-credential", llm, settings, "test", emit)
    assert result.report is not None
    everything=json.dumps([r["messages"] for r in llm.requests])
    assert "fpk_live_51NzQ8vXk2T9rLmW4pYc3Hd7sQ" not in everything
    assert "[REDACTED:api_key]" in everything
    assert sum(1 for e in events if e["type"]=="guardrail" and e["kind"]=="redaction")==2


async def test_injected_text_is_wrapped_as_untrusted(settings):
    llm=SequenceLlm([toolResponse("searchLogs", {"query": "checkout failed", "service": "checkout-api", "level": "ERROR"}),
                     toolResponse("submitReport", SCENARIOS["prompt-injection"].scriptedReport, "c2")])
    events, emit=collector()
    await runAgent("prompt-injection", llm, settings, "test", emit)
    toolMessage=[m for m in llm.requests[1]["messages"] if m["role"]=="tool"][0]
    assert json.loads(toolMessage["content"])["securityNotice"]
    assert any(e["type"]=="guardrail" and e["kind"]=="injection" for e in events)


async def test_step_limit_forces_a_report(settings):
    settings.maxSteps=3
    counter={"n": 0}

    def wander(model):
        counter["n"]+=1
        return toolResponse("queryMetrics", {"service": "checkout-api", "metric": "error_rate", "minutes": 10+counter["n"]}, f"c{counter['n']}")

    report=SCENARIOS["latency-bad-deploy"].scriptedReport

    class StubbornThenSubmit(SequenceLlm):
        async def complete(self, messages, tools, model, toolChoice=None):
            self.requests.append({"toolChoice": toolChoice})
            if toolChoice:
                return toolResponse("submitReport", report, "final")
            return wander(model)

    llm=StubbornThenSubmit([None])
    events, emit=collector()
    result=await runAgent("latency-bad-deploy", llm, settings, "test", emit)
    assert result.report is not None and result.stoppedReason=="step limit"
    assert llm.requests[-1]["toolChoice"]["function"]["name"]=="submitReport"


async def test_invalid_report_is_rejected_then_fixed(settings):
    bad=copy.deepcopy(SCENARIOS["double-charge"].scriptedReport)
    bad["severity"]="CATASTROPHIC"
    llm=SequenceLlm([toolResponse("submitReport", bad, "c1"), toolResponse("submitReport", SCENARIOS["double-charge"].scriptedReport, "c2")])
    events, emit=collector()
    result=await runAgent("double-charge", llm, settings, "test", emit)
    assert result.report is not None
    assert any(e["type"]=="guardrail" and e["kind"]=="validation" for e in events)


async def test_repeated_call_is_not_rerun(settings):
    call=toolResponse("listDeploys", {"hours": 24})
    llm=SequenceLlm([call, call, toolResponse("submitReport", SCENARIOS["latency-bad-deploy"].scriptedReport, "c9")])
    events, emit=collector()
    await runAgent("latency-bad-deploy", llm, settings, "test", emit)
    results=[e for e in events if e["type"]=="tool_result"]
    assert "already made this exact call" in results[1]["output"]


async def test_model_errors_end_the_run_cleanly(settings):
    class Broken:
        label="broken"

        async def complete(self, *a, **k):
            raise LlmError("RateLimitError: 429")

    events, emit=collector()
    result=await runAgent("latency-bad-deploy", Broken(), settings, "test", emit)
    assert result.report is None and result.stoppedReason=="model error"
    assert events[-1]["type"]=="error"


async def test_low_confidence_escalates_to_larger_model(settings):
    settings.escalationModel="big-model"
    weak=copy.deepcopy(SCENARIOS["latency-bad-deploy"].scriptedReport)
    weak["confidence"]=0.3
    strong=SCENARIOS["latency-bad-deploy"].scriptedReport

    llm=SequenceLlm([lambda m: toolResponse("submitReport", weak if m!="big-model" else strong)])
    events, emit=collector()
    result, first=await investigate("latency-bad-deploy", llm, settings, emit)
    assert first.report.confidence==0.3 and result.model=="big-model" and result.report.confidence==0.9
    assert any(e["type"]=="escalation" for e in events)


async def test_usage_and_cost_are_tracked(settings):
    settings.modelPrices["test"]=[1.0, 10.0]
    llm=SequenceLlm([toolResponse("submitReport", SCENARIOS["latency-bad-deploy"].scriptedReport, tokensIn=1000, tokensOut=100)])
    events, emit=collector()
    result=await runAgent("latency-bad-deploy", llm, settings, "test", emit)
    assert result.inputTokens==1000 and abs(result.costUsd-0.002)<1e-9


async def test_text_only_reply_gets_a_nudge(settings):
    llm=SequenceLlm([LlmResponse("I think it is the deploy.", [], 10, 5), toolResponse("submitReport", SCENARIOS["latency-bad-deploy"].scriptedReport)])
    events, emit=collector()
    result=await runAgent("latency-bad-deploy", llm, settings, "test", emit)
    assert result.report is not None
    assert any(e["type"]=="thinking" for e in events)
