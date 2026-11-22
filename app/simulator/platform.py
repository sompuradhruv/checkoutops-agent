"""A simulated checkout platform that produces telemetry.

Nothing here talks to a real system. Each scenario starts from the same healthy
baseline (seeded, so every run is identical) and then injects one fault.
Deterministic telemetry is what makes the evals meaningful: every scenario has
one known root cause.

Time model: a 120-minute window at 1-minute resolution. Index 0 is 119 minutes
ago, index 119 is "now".
"""
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

NOW=datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
WINDOW=120
BASELINE_END=60  # minutes [0, 60) are treated as the healthy baseline

# Request path: checkout-web -> checkout-api -> {pricing, inventory, payment, order}
# payment-service -> Fabrikam Payments (external card processor, fictional)
SERVICES={
    "checkout-web": {"rpm": 900, "p50": 60, "p95": 180, "err": 0.002,
                     "role": "Storefront backend-for-frontend; calls checkout-api"},
    "checkout-api": {"rpm": 850, "p50": 90, "p95": 210, "err": 0.003,
                     "role": "Checkout orchestration: cart pricing, reservation, payment, order creation"},
    "pricing-service": {"rpm": 2600, "p50": 12, "p95": 35, "err": 0.001,
                        "role": "Returns prices and discounts for SKUs"},
    "inventory-service": {"rpm": 1700, "p50": 15, "p95": 45, "err": 0.001,
                          "role": "Reserves stock for items in a cart"},
    "payment-service": {"rpm": 420, "p50": 240, "p95": 650, "err": 0.004,
                        "role": "Charges cards through Fabrikam Payments (external provider)"},
    "order-service": {"rpm": 400, "p50": 40, "p95": 120, "err": 0.002,
                      "role": "Creates orders and order history; also logs support tickets"},
}

STANDARD_METRICS=["requests_per_min", "error_rate", "latency_p50_ms", "latency_p95_ms"]


def timeAt(index):
    return NOW-timedelta(minutes=WINDOW-1-index)


def fmtTime(index):
    return timeAt(index).strftime("%Y-%m-%dT%H:%MZ")


def minutesAgo(index):
    return WINDOW-1-index


def indexForMinutesAgo(m):
    return WINDOW-1-m


@dataclass
class LogLine:
    index: int
    service: str
    level: str
    message: str
    traceId: str=""


@dataclass
class Span:
    service: str
    operation: str
    durationMs: int
    status: str="ok"
    note: str=""


@dataclass
class Trace:
    traceId: str
    index: int
    rootService: str
    rootOperation: str
    durationMs: int
    status: str
    spans: list


@dataclass
class Deploy:
    deployId: str
    service: str
    version: str
    minutesAgo: int
    author: str
    summary: str
    diff: str
    status: str="succeeded"


@dataclass
class Alert:
    title: str
    service: str
    metric: str
    threshold: float
    comparison: str  # "above" | "below"
    firedIndex: int
    unit: str=""


@dataclass
class Telemetry:
    metrics: dict=field(default_factory=dict)  # service -> metric -> list[float]
    logs: list=field(default_factory=list)
    traces: list=field(default_factory=list)
    deploys: list=field(default_factory=list)
    configs: dict=field(default_factory=dict)
    alert: Alert|None=None

    def series(self, service, metric):
        return self.metrics.get(service, {}).get(metric)

    def setSeries(self, service, metric, values):
        self.metrics.setdefault(service, {})[metric]=values


class Builder:
    """Helpers that scenarios use to inject faults into the baseline."""

    def __init__(self, seed):
        self.rng=random.Random(seed)
        self.tel=Telemetry()
        self.traceCounter=0
        self.orderCounter=4000

    # ---------- ids ----------
    def traceId(self):
        self.traceCounter+=1
        return f"tr_{self.rng.getrandbits(40):010x}"

    def orderId(self):
        self.orderCounter+=self.rng.randint(1, 7)
        return f"ord_{self.orderCounter}{self.rng.choice('ABCDEFGHJKMNPQRSTVWXYZ')}"

    # ---------- metrics ----------
    def noisy(self, base, jitter=0.05, n=WINDOW):
        return [max(0.0, base*(1+self.rng.gauss(0, jitter))) for _ in range(n)]

    def baselineMetrics(self):
        for name, s in SERVICES.items():
            self.tel.setSeries(name, "requests_per_min", self.noisy(s["rpm"], 0.04))
            self.tel.setSeries(name, "error_rate", [max(0.0, s["err"]*(1+self.rng.gauss(0, 0.25))) for _ in range(WINDOW)])
            self.tel.setSeries(name, "latency_p50_ms", self.noisy(s["p50"], 0.05))
            self.tel.setSeries(name, "latency_p95_ms", self.noisy(s["p95"], 0.07))
        self.tel.setSeries("checkout-api", "checkout_success_rate", [min(1.0, 0.985+self.rng.gauss(0, 0.003)) for _ in range(WINDOW)])
        self.tel.setSeries("checkout-api", "pricing_calls_per_request", self.noisy(1.0, 0.02))
        self.tel.setSeries("checkout-api", "instance_count", [6.0]*WINDOW)
        self.tel.setSeries("payment-service", "provider_requests_per_min", self.noisy(430, 0.04))
        self.tel.setSeries("payment-service", "provider_error_rate", [max(0.0, 0.002*(1+self.rng.gauss(0, 0.3))) for _ in range(WINDOW)])
        self.tel.setSeries("payment-service", "duplicate_charges_per_min", [0.0]*WINDOW)
        self.tel.setSeries("payment-service", "auth_failures_per_min", [0.0]*WINDOW)
        self.tel.setSeries("payment-service", "charges_missing_idempotency_key_per_min", [0.0]*WINDOW)

    def shift(self, service, metric, start, target, rampMinutes=3, jitter=0.06):
        """From index `start`, move the series to around `target` over `rampMinutes`."""
        values=self.tel.series(service, metric)
        before=sum(values[:BASELINE_END])/BASELINE_END
        for i in range(start, WINDOW):
            progress=min(1.0, (i-start+1)/max(1, rampMinutes))
            level=before+(target-before)*progress
            values[i]=max(0.0, level*(1+self.rng.gauss(0, jitter)))

    def scaleAll(self, metric, start, factor, services=None, rampMinutes=5):
        for name in services or SERVICES:
            values=self.tel.series(name, metric)
            base=sum(values[:BASELINE_END])/BASELINE_END
            self.shift(name, metric, start, base*factor, rampMinutes)

    # ---------- logs ----------
    def log(self, index, service, level, message, traceId=""):
        self.tel.logs.append(LogLine(index, service, level, message, traceId))

    def baselineLogs(self):
        templates={
            "checkout-web": [("INFO", "GET /checkout 200 {ms}ms"), ("INFO", "POST /checkout/submit 200 {ms}ms")],
            "checkout-api": [("INFO", "POST /checkout/price 200 {ms}ms items={n}"), ("INFO", "POST /checkout/complete 200 {ms}ms orderId={order}")],
            "pricing-service": [("INFO", "POST /prices/batch 200 {ms}ms skus={n}")],
            "inventory-service": [("INFO", "POST /reservations 201 {ms}ms skus={n}")],
            "payment-service": [("INFO", "charge succeeded orderId={order} amount=${amt} provider=fabrikam {ms}ms")],
            "order-service": [("INFO", "order created orderId={order} items={n}")],
        }
        for i in range(WINDOW):
            for service, opts in templates.items():
                if self.rng.random()<0.45:
                    level, tpl=self.rng.choice(opts)
                    s=SERVICES[service]
                    msg=tpl.format(ms=int(s["p50"]*(0.7+self.rng.random()*0.8)), n=self.rng.randint(1, 6),
                                   order=self.orderId(), amt=f"{self.rng.randint(12, 240)}.{self.rng.randint(0, 99):02d}")
                    self.log(i, service, level, msg)
            # background noise that is NOT the incident: agents must learn to ignore it
            if self.rng.random()<0.06:
                self.log(i, "order-service", "WARN", f"slow query on orders_history {self.rng.randint(210, 340)}ms (threshold 200ms)")
            if self.rng.random()<0.04:
                self.log(i, "checkout-web", "WARN", "client closed connection before response (499) path=/checkout")
            if self.rng.random()<0.03:
                self.log(i, "pricing-service", "WARN", "cache miss ratio 0.21 above 0.20 for region=us-east")

    # ---------- traces ----------
    def addTrace(self, index, rootService, rootOperation, spans, status="ok"):
        duration=sum(sp.durationMs for sp in spans)+self.rng.randint(5, 25)
        tr=Trace(self.traceId(), index, rootService, rootOperation, duration, status, spans)
        self.tel.traces.append(tr)
        return tr

    def baselineTraces(self):
        for _ in range(24):
            i=self.rng.randint(0, WINDOW-1)
            spans=[
                Span("checkout-web", "POST /checkout/submit", self.rng.randint(8, 20)),
                Span("checkout-api", "POST /checkout/complete", self.rng.randint(15, 30)),
                Span("pricing-service", "POST /prices/batch", self.rng.randint(8, 18)),
                Span("inventory-service", "POST /reservations", self.rng.randint(10, 22)),
                Span("payment-service", "POST /charges", self.rng.randint(180, 320)),
                Span("order-service", "POST /orders", self.rng.randint(25, 50)),
            ]
            self.addTrace(i, "checkout-web", "POST /checkout/submit", spans)

    # ---------- deploys and config ----------
    def baselineDeploys(self):
        self.tel.deploys+=[
            Deploy("d-4790", "order-service", "v3.9.1", 6*60, "mgarcia", "Add CSV export for order history",
                   "--- a/OrderExportController.cs\n+++ b/OrderExportController.cs\n+    [HttpGet(\"export\")]\n+    public async Task<IActionResult> Export(...){ ... }"),
            Deploy("d-4772", "pricing-service", "v2.2.0", 20*60, "tchen", "Holiday discount rules engine update",
                   "--- a/rules/holiday.json\n+++ b/rules/holiday.json\n+  {\"campaign\": \"bf26\", \"percentOff\": 20}"),
        ]

    def baselineConfigs(self):
        self.tel.configs={
            "checkout-web": {"paymentClient": {"timeoutMs": 1500, "retry": {"attempts": 0}}, "featureFlags": {"bf26-homepage-banner": False}},
            "checkout-api": {"autoscale": {"min": 6, "max": 20, "targetCpu": 0.6}, "timeouts": {"pricingMs": 800, "paymentMs": 5000}},
            "pricing-service": {"cache": {"ttlSeconds": 300}},
            "inventory-service": {"reservationTtlMinutes": 15},
            "payment-service": {
                "provider": "Fabrikam Payments",
                "retryPolicy": {"maxAttempts": 2, "backoff": "exponential", "baseDelayMs": 200},
                "circuitBreaker": {"enabled": True, "failureRateThreshold": 0.5},
                "secondaryProvider": {"name": "Northwind Pay", "failoverEnabled": True},
                "secrets": [{"name": "FabrikamApiKey", "source": "Azure Key Vault (kv-checkout-prod)", "expiresAt": "2027-03-01T00:00Z", "rotation": "automatic"}],
                "idempotency": {"required": False, "headerName": "Idempotency-Key"},
            },
            "order-service": {"db": "orders-sql-prod"},
        }

    def baseline(self):
        self.baselineMetrics()
        self.baselineLogs()
        self.baselineTraces()
        self.baselineDeploys()
        self.baselineConfigs()
        return self

    def finish(self):
        self.tel.logs.sort(key=lambda l: l.index)
        self.tel.traces.sort(key=lambda t: t.index)
        self.tel.deploys.sort(key=lambda d: d.minutesAgo)
        return self.tel
