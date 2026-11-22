from app.config import Settings
from app.demo.protection import DailyBudget, DemoGuard, TokenBucket


def test_token_bucket_limits_and_refills():
    bucket=TokenBucket(2, 1/60)
    assert bucket.take("ip", now=0)[0]
    assert bucket.take("ip", now=1)[0]
    ok, wait=bucket.take("ip", now=2)
    assert not ok and wait>0
    assert bucket.take("other", now=2)[0]
    assert bucket.take("ip", now=70)[0]


def test_daily_budget():
    b=DailyBudget(1.0)
    assert b.canSpend()
    b.add(1.2)
    assert not b.canSpend()


def test_guard_refuses_without_model():
    guard=DemoGuard(Settings(llmProvider="none"))
    allowed, reason=guard.admitLive("ip")
    assert not allowed and "No model" in reason


def test_settings_price_lookup_handles_suffixes():
    s=Settings()
    assert s.priceFor("gpt-5-mini-eastus")==s.modelPrices["gpt-5-mini"]
    assert s.priceFor("mystery-model")==[1.25, 10.00]
