import os

os.environ.setdefault("LLM_PROVIDER", "scripted")
os.environ.setdefault("AGENT_MODEL", "scripted")

import json  # noqa: E402

import pytest  # noqa: E402

from app.agent.llm import LlmResponse, ToolCall  # noqa: E402
from app.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(llmProvider="scripted", agentModel="scripted", replayDir=str(tmp_path/"replays"),
                    evalResultsDir=str(tmp_path/"evals"), replaySpeed=0, liveRunsPerIpPerHour=100)


class SequenceLlm:
    """Test double: returns queued responses in order, records every request."""
    label="sequence"

    def __init__(self, responses):
        self.responses=list(responses)
        self.requests=[]

    async def complete(self, messages, tools, model, toolChoice=None):
        self.requests.append({"messages": json.loads(json.dumps(messages)), "toolChoice": toolChoice, "model": model})
        item=self.responses.pop(0) if len(self.responses)>1 else self.responses[0]
        return item(model) if callable(item) else item


def toolResponse(name, args, callId="c1", tokensIn=100, tokensOut=20):
    return LlmResponse("", [ToolCall(callId, name, json.dumps(args))], tokensIn, tokensOut, "test")


def collector():
    events=[]

    async def emit(e):
        events.append(e)
    return events, emit
