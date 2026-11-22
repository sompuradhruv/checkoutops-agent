from app.simulator.platform import BASELINE_END, Builder
from app.simulator.scenarios import SCENARIOS, buildTelemetry
from app.tools.telemetry import detectChange


def test_telemetry_is_deterministic():
    for sc in SCENARIOS.values():
        a=Builder(sc.seed).baseline()
        sc.build(a)
        b=Builder(sc.seed).baseline()
        sc.build(b)
        assert a.finish().metrics==b.finish().metrics
        assert [l.message for l in a.tel.logs]==[l.message for l in b.tel.logs]


def test_each_alert_breaches_only_after_the_fault():
    for sid, sc in SCENARIOS.items():
        t=buildTelemetry(sid)
        a=t.alert
        values=t.series(a.service, a.metric)
        breached=(lambda v: v>a.threshold) if a.comparison=="above" else (lambda v: v<a.threshold)
        assert not any(breached(v) for v in values[:BASELINE_END]), sid
        assert breached(values[a.firedIndex]), sid
        assert breached(values[-1]), sid


def test_change_detection_finds_the_injected_start():
    t=buildTelemetry("latency-bad-deploy")
    change=detectChange(t.series("checkout-api", "latency_p95_ms"))
    assert change and change["direction"]=="up" and 23<=change["minutesAgo"]<=26
    assert detectChange(t.series("order-service", "latency_p95_ms")) is None


def test_scenarios_have_complete_ground_truth():
    for sc in SCENARIOS.values():
        assert sc.groundTruth and sc.concept and sc.expected.acceptableActions
        assert sc.scriptedReport["rootCause"]["category"]==sc.expected.category
