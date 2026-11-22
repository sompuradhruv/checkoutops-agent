"""Protection for a public demo that spends real money on every live run.

- Per-visitor rate limit (token bucket per client IP).
- Global concurrency cap on live runs.
- Daily spend cap in USD, computed from token usage. Once hit, everyone gets replays.

State is in memory, which is fine for one replica (the deploy guide pins
max replicas to 1). With several replicas this would move to Redis.
"""
import time
from datetime import datetime, timezone


class TokenBucket:
    def __init__(self, capacity, refillPerSecond):
        self.capacity=capacity
        self.refill=refillPerSecond
        self.buckets={}  # key -> (tokens, lastTime)

    def take(self, key, now=None):
        now=time.monotonic() if now is None else now
        tokens, last=self.buckets.get(key, (self.capacity, now))
        tokens=min(self.capacity, tokens+(now-last)*self.refill)
        if tokens<1:
            self.buckets[key]=(tokens, now)
            return False, int((1-tokens)/self.refill)+1
        self.buckets[key]=(tokens-1, now)
        return True, 0

    def giveBack(self, key):
        tokens, last=self.buckets.get(key, (self.capacity, time.monotonic()))
        self.buckets[key]=(min(self.capacity, tokens+1), last)


class DailyBudget:
    def __init__(self, limitUsd):
        self.limitUsd=limitUsd
        self.day=self.today()
        self.spentUsd=0.0

    @staticmethod
    def today():
        return datetime.now(timezone.utc).date()

    def roll(self):
        if self.today()!=self.day:
            self.day=self.today()
            self.spentUsd=0.0

    def canSpend(self):
        self.roll()
        return self.spentUsd<self.limitUsd

    def add(self, usd):
        self.roll()
        self.spentUsd+=usd

    def status(self):
        self.roll()
        return {"spentUsd": round(self.spentUsd, 4), "limitUsd": self.limitUsd}


class DemoGuard:
    def __init__(self, settings):
        self.settings=settings
        perHour=max(1, settings.liveRunsPerIpPerHour)
        self.limiter=TokenBucket(perHour, perHour/3600)
        self.budget=DailyBudget(settings.dailyBudgetUsd)
        self.activeLive=0

    def admitLive(self, clientKey):
        """Return (allowed, reason). Consumes a rate-limit token when allowed."""
        if not self.settings.liveConfigured:
            return False, "No model is configured on this server, so runs play back recordings."
        if not self.budget.canSpend():
            return False, "Today's demo budget is used up, so runs play back recordings. Live runs resume tomorrow (UTC)."
        if self.activeLive>=self.settings.maxConcurrentLiveRuns:
            return False, "Too many live runs right now; playing back a recording instead."
        ok, wait=self.limiter.take(clientKey)
        if not ok:
            return False, f"Live run limit reached for your connection; playing back a recording. Try live again in ~{max(1, wait//60)} min."
        return True, ""
