"""LLM clients behind one small interface.

The agent loop only knows `complete(messages, tools, model, toolChoice)`. That
keeps the provider swappable: Azure OpenAI in Microsoft Foundry for the hosted
demo, OpenAI directly as a fallback, and a scripted fake for tests and offline
UI work.

Azure auth supports two modes:
- API key (simple, fine for local dev)
- Managed identity via Microsoft Entra ID (no secret at all; used when hosted
  on Azure Container Apps). The token provider fetches and refreshes tokens.
"""
import json
from dataclasses import dataclass, field

from app.simulator.scenarios import getScenario


class LlmError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LlmResponse:
    content: str
    toolCalls: list=field(default_factory=list)
    inputTokens: int=0
    outputTokens: int=0
    model: str=""

    def assistantMessage(self):
        msg={"role": "assistant", "content": self.content or None}
        if self.toolCalls:
            msg["tool_calls"]=[{"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}} for tc in self.toolCalls]
        return msg


class OpenAiCompatibleClient:
    """Chat Completions with tools. Works for OpenAI and the Azure OpenAI v1 endpoint."""

    def __init__(self, client, label, settings):
        self.client=client
        self.label=label
        self.settings=settings

    async def complete(self, messages, tools, model, toolChoice=None):
        kwargs={"model": model, "messages": messages, "tools": tools,
                "max_completion_tokens": self.settings.maxCompletionTokens, "timeout": self.settings.llmTimeoutSeconds}
        if toolChoice:
            kwargs["tool_choice"]=toolChoice
        if self.settings.temperature is not None:  # GPT-5 family reasoning models only accept the default
            kwargs["temperature"]=self.settings.temperature
        if self.settings.reasoningEffort:
            kwargs["reasoning_effort"]=self.settings.reasoningEffort
        try:
            resp=await self.client.chat.completions.create(**kwargs)
        except Exception as e:  # rate limits, auth, timeouts: surfaced to the run, never crash the server
            raise LlmError(f"{type(e).__name__}: {str(e)[:300]}") from e
        choice=resp.choices[0].message
        calls=[ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}") for tc in (choice.tool_calls or [])]
        usage=resp.usage
        return LlmResponse(choice.content or "", calls, usage.prompt_tokens if usage else 0,
                           usage.completion_tokens if usage else 0, resp.model or model)


class ScriptedLlm:
    """Deterministic fake: replays a scenario's scripted tool calls, then submits its reference report.

    It is NOT a model and the UI labels it as such. It exists so tests and local UI work need no key.
    """

    label="Scripted (no LLM)"

    def __init__(self, scenarioId):
        self.scenario=getScenario(scenarioId)

    async def complete(self, messages, tools, model, toolChoice=None):
        step=sum(1 for m in messages if m["role"]=="assistant")
        steps=self.scenario.scriptedSteps
        forced=isinstance(toolChoice, dict)
        if step<len(steps) and not forced:
            name, args=steps[step]
            return LlmResponse("", [ToolCall(f"call_{step}", name, json.dumps(args))], 0, 0, "scripted")
        return LlmResponse("", [ToolCall(f"call_{step}", "submitReport", json.dumps(self.scenario.scriptedReport))], 0, 0, "scripted")


_azureClient=None


def azureClient(settings):
    global _azureClient
    if _azureClient is None:
        from openai import AsyncOpenAI
        endpoint=settings.azureEndpoint.rstrip("/")
        baseUrl=endpoint if endpoint.endswith("/openai/v1") else endpoint+"/openai/v1"
        if settings.azureApiKey:
            apiKey=settings.azureApiKey
        else:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
            apiKey=get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        _azureClient=AsyncOpenAI(base_url=baseUrl+"/", api_key=apiKey, max_retries=2)
    return _azureClient


def makeLlm(settings, scenarioId):
    provider=settings.llmProvider
    if provider=="scripted":
        return ScriptedLlm(scenarioId)
    if provider=="azure":
        auth="managed identity" if not settings.azureApiKey else "API key"
        return OpenAiCompatibleClient(azureClient(settings), f"Azure OpenAI ({auth})", settings)
    if provider=="openai":
        from openai import AsyncOpenAI
        return OpenAiCompatibleClient(AsyncOpenAI(api_key=settings.openaiApiKey, max_retries=2), "OpenAI", settings)
    raise LlmError("No LLM configured. Set LLM_PROVIDER=azure (or openai / scripted).")
