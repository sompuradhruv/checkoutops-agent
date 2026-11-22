"""The agent loop, written by hand so every step is explainable.

    alert -> [model picks a tool -> our code validates and runs it -> guardrails -> result back to model] x N -> report

Stop conditions: the model submits a report, or we hit the step limit or the
token budget, at which point the model is forced to submit its best report.
Optional escalation: if a small model is not confident, rerun with a larger one.
"""
import json
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.agent.guardrails import SECURITY_NOTICE, detectInjection, maskSecrets
from app.agent.llm import LlmError
from app.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, alertMessage
from app.agent.report import IncidentReport
from app.simulator.scenarios import getScenario
from app.tools.registry import SUBMIT_REPORT, executeTool, toolSchemas

MAX_TOOL_OUTPUT_CHARS=6000
FORCE_SUBMIT={"type": "function", "function": {"name": SUBMIT_REPORT}}


@dataclass
class AgentResult:
    model: str
    report: IncidentReport|None=None
    steps: int=0
    toolCalls: int=0
    inputTokens: int=0
    outputTokens: int=0
    costUsd: float=0.0
    latencyMs: int=0
    stoppedReason: str=""
    error: str|None=None
    guardrailEvents: list=field(default_factory=list)


def costOf(settings, model, inputTokens, outputTokens):
    pin, pout=settings.priceFor(model)
    return (inputTokens*pin+outputTokens*pout)/1_000_000


def parseReport(rawArgs):
    data=json.loads(rawArgs or "{}")
    if isinstance(data, dict) and "report" in data and isinstance(data["report"], dict) and "severity" not in data:
        data=data["report"]  # some models wrap the payload
    return IncidentReport.model_validate(data)


async def runAgent(scenarioId, llm, settings, model, emit, extraContext=None):
    scenario=getScenario(scenarioId)
    alert=scenario.telemetry().alert
    tools=toolSchemas()
    messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": alertMessage(alert, extraContext)}]
    result=AgentResult(model=model)
    started=time.monotonic()
    seenCalls=set()
    nudged=False
    forceSubmit=False
    reportErrors=0

    async def call(toolChoice=None):
        resp=await llm.complete(messages, tools, model, toolChoice)
        result.inputTokens+=resp.inputTokens
        result.outputTokens+=resp.outputTokens
        result.costUsd=costOf(settings, model, result.inputTokens, result.outputTokens)
        await emit({"type": "usage", "model": model, "inputTokens": result.inputTokens, "outputTokens": result.outputTokens,
                    "costUsd": round(result.costUsd, 5)})
        return resp

    try:
        while True:
            overBudget=result.inputTokens+result.outputTokens>settings.maxRunTokens
            if not forceSubmit and (result.steps>=settings.maxSteps or overBudget):
                forceSubmit=True
                reason="token budget" if overBudget else "step limit"
                result.stoppedReason=reason
                await emit({"type": "guardrail", "kind": "limit", "detail": f"Reached the {reason}; asking the model for its best report now."})
                messages.append({"role": "user", "content": f"You have reached the {reason}. Call submitReport now with your best assessment and an honest confidence."})
            if forceSubmit and (reportErrors>=3 or result.steps>=settings.maxSteps+3):
                result.stoppedReason=result.stoppedReason or "no valid report"
                result.error="The model did not produce a valid report."
                await emit({"type": "error", "message": result.error})
                break

            result.steps+=1
            resp=await call(FORCE_SUBMIT if forceSubmit else None)
            if resp.content and resp.content.strip():
                await emit({"type": "thinking", "step": result.steps, "text": resp.content.strip()[:800]})

            if not resp.toolCalls:
                messages.append(resp.assistantMessage())
                if nudged:
                    forceSubmit=True
                else:
                    nudged=True
                    messages.append({"role": "user", "content": "Continue the investigation with tools, or call submitReport if you have enough evidence."})
                continue

            messages.append(resp.assistantMessage())
            done=False
            for tc in resp.toolCalls:
                if tc.name==SUBMIT_REPORT:
                    try:
                        report=parseReport(tc.arguments)
                    except (json.JSONDecodeError, ValidationError, ValueError) as e:
                        reportErrors+=1
                        detail=str(e)[:700]
                        await emit({"type": "guardrail", "kind": "validation", "detail": f"Report rejected by schema validation; model asked to fix it. {detail[:200]}"})
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"error": "report failed validation", "details": detail})})
                        if reportErrors>=2:
                            forceSubmit=True
                        continue
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "Report accepted."})
                    result.report=report
                    result.stoppedReason=result.stoppedReason or "report submitted"
                    await emit({"type": "report", "model": model, "report": report.model_dump()})
                    done=True
                    continue
                if done:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "{\"skipped\": \"report already submitted\"}"})
                    continue

                result.toolCalls+=1
                try:
                    args=json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    args={"_raw": tc.arguments[:200]}
                await emit({"type": "tool_call", "step": result.steps, "id": tc.id, "name": tc.name, "args": args})
                key=(tc.name, json.dumps(args, sort_keys=True))
                if key in seenCalls:
                    output={"note": "You already made this exact call. Use the earlier result or try something different."}
                    ok=False
                else:
                    seenCalls.add(key)
                    output, ok=executeTool(scenarioId, tc.name, tc.arguments)
                text=json.dumps(output, separators=(",", ":"))
                text, redacted=maskSecrets(text)
                injections=detectInjection(text)
                if redacted:
                    detail=f"Redacted {len(redacted)} secret value(s) ({', '.join(sorted(set(redacted)))}) from {tc.name} output before sending it to the model."
                    result.guardrailEvents.append(detail)
                    await emit({"type": "guardrail", "kind": "redaction", "detail": detail})
                if injections:
                    detail=f"Possible prompt injection in {tc.name} output; marked as untrusted data: \"{injections[0][:160]}\""
                    result.guardrailEvents.append(detail)
                    await emit({"type": "guardrail", "kind": "injection", "detail": detail})
                    text=json.dumps({"securityNotice": SECURITY_NOTICE, "untrustedToolOutput": json.loads(text)}, separators=(",", ":"))
                if len(text)>MAX_TOOL_OUTPUT_CHARS:
                    text=text[:MAX_TOOL_OUTPUT_CHARS]+"...[truncated]"
                await emit({"type": "tool_result", "step": result.steps, "id": tc.id, "name": tc.name, "ok": ok,
                            "chars": len(text), "output": text, "redactions": len(redacted), "injection": bool(injections)})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            if done:
                break
    except LlmError as e:
        result.error=str(e)
        result.stoppedReason="model error"
        await emit({"type": "error", "message": f"Model call failed: {e}"})
    result.latencyMs=int((time.monotonic()-started)*1000)
    return result


async def investigate(scenarioId, llm, settings, emit, modelOverride=None, allowEscalation=True):
    """Run with the main model; escalate to a larger model if the report is missing or low-confidence."""
    model=modelOverride or settings.agentModel
    await emit({"type": "agent_started", "model": model, "promptVersion": PROMPT_VERSION})
    first=await runAgent(scenarioId, llm, settings, model, emit)
    escalateTo=settings.escalationModel if allowEscalation and not modelOverride else ""
    lowConfidence=first.report is None or first.report.confidence<settings.escalationConfidence
    if not escalateTo or not lowConfidence or first.stoppedReason=="model error":
        return first, None
    reason="no valid report" if first.report is None else f"confidence {first.report.confidence:.2f} < {settings.escalationConfidence}"
    await emit({"type": "escalation", "fromModel": model, "toModel": escalateTo, "reason": reason})
    context=None
    if first.report is not None:
        context=(f"A first pass by a smaller model was not confident (confidence {first.report.confidence:.2f}). Its hypothesis: "
                 f"{first.report.rootCause.category} / {first.report.rootCause.culprit}. Verify or reject it with evidence.")
    second=await runAgent(scenarioId, llm, settings, escalateTo, emit, extraContext=context)
    return second, first
