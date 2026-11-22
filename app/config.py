"""Settings, read once from environment variables.

Everything that changes between local dev and the hosted demo lives here,
so the rest of the code never reads os.environ directly.
"""
import json
import os
from dataclasses import dataclass, field


def envStr(name, default=""):
    return os.environ.get(name, default).strip()


def envInt(name, default):
    raw=os.environ.get(name)
    return int(raw) if raw else default


def envFloat(name, default):
    raw=os.environ.get(name)
    return float(raw) if raw else default


def envBool(name, default=False):
    raw=os.environ.get(name)
    if raw is None or raw=="":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# USD per 1M tokens (input, output). Estimates only: check the Azure pricing page
# for your region and override with MODEL_PRICES='{"my-deployment":[0.25,2.0]}'.
defaultPrices={
    "gpt-5-mini": [0.25, 2.00],
    "gpt-5": [1.25, 10.00],
    "gpt-5.4-mini": [0.25, 2.00],
    "gpt-4.1-mini": [0.40, 1.60],
    "gpt-4.1": [2.00, 8.00],
    "scripted": [0.0, 0.0],
}


@dataclass
class Settings:
    # azure | openai | scripted | none
    llmProvider: str="none"
    azureEndpoint: str=""
    azureApiKey: str=""
    azureUseManagedIdentity: bool=False
    openaiApiKey: str=""
    agentModel: str="gpt-5-mini"
    escalationModel: str=""
    escalationConfidence: float=0.6
    temperature: float|None=None
    reasoningEffort: str=""
    maxSteps: int=14
    maxCompletionTokens: int=4000
    maxRunTokens: int=150_000
    llmTimeoutSeconds: float=60.0
    # demo protection
    liveRunsPerIpPerHour: int=5
    maxConcurrentLiveRuns: int=3
    dailyBudgetUsd: float=2.0
    replaySpeed: float=1.0
    replayDir: str="replays"
    evalResultsDir: str="evals/results"
    modelPrices: dict=field(default_factory=lambda: dict(defaultPrices))

    @property
    def liveConfigured(self):
        if self.llmProvider=="azure":
            return bool(self.azureEndpoint) and (bool(self.azureApiKey) or self.azureUseManagedIdentity)
        if self.llmProvider=="openai":
            return bool(self.openaiApiKey)
        return self.llmProvider=="scripted"

    def priceFor(self, model):
        if model in self.modelPrices:
            return self.modelPrices[model]
        # deployment names often carry a suffix, e.g. "gpt-5-mini-eastus"
        for name in sorted(self.modelPrices, key=len, reverse=True):
            if model.startswith(name):
                return self.modelPrices[name]
        return [1.25, 10.00]  # unknown model: assume an expensive one so the budget stays safe


def loadSettings():
    prices=dict(defaultPrices)
    if envStr("MODEL_PRICES"):
        prices.update(json.loads(envStr("MODEL_PRICES")))
    temperature=envStr("LLM_TEMPERATURE")
    return Settings(
        llmProvider=envStr("LLM_PROVIDER", "none").lower(),
        azureEndpoint=envStr("AZURE_OPENAI_ENDPOINT"),
        azureApiKey=envStr("AZURE_OPENAI_API_KEY"),
        azureUseManagedIdentity=envBool("AZURE_USE_MANAGED_IDENTITY"),
        openaiApiKey=envStr("OPENAI_API_KEY"),
        agentModel=envStr("AGENT_MODEL", "gpt-5-mini"),
        escalationModel=envStr("ESCALATION_MODEL"),
        escalationConfidence=envFloat("ESCALATION_CONFIDENCE", 0.6),
        temperature=float(temperature) if temperature else None,
        reasoningEffort=envStr("LLM_REASONING_EFFORT"),
        maxSteps=envInt("MAX_STEPS", 14),
        maxCompletionTokens=envInt("MAX_COMPLETION_TOKENS", 4000),
        maxRunTokens=envInt("MAX_RUN_TOKENS", 150_000),
        llmTimeoutSeconds=envFloat("LLM_TIMEOUT_SECONDS", 60.0),
        liveRunsPerIpPerHour=envInt("LIVE_RUNS_PER_IP_PER_HOUR", 5),
        maxConcurrentLiveRuns=envInt("MAX_CONCURRENT_LIVE_RUNS", 3),
        dailyBudgetUsd=envFloat("DAILY_BUDGET_USD", 2.0),
        replaySpeed=envFloat("REPLAY_SPEED", 1.0),
        replayDir=envStr("REPLAY_DIR", "replays"),
        evalResultsDir=envStr("EVAL_RESULTS_DIR", "evals/results"),
        modelPrices=prices,
    )
