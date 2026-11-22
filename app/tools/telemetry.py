"""Read-only investigation tools over the simulated telemetry.

Design rule: tools summarise before they return. Raw minute-level series and
thousands of log lines would blow the context window and cost a lot, so
metrics come back as 5-minute buckets plus a detected change point, and log
search returns counts, top patterns and a few distinct samples.
"""
import copy
import re
from collections import Counter

from app.simulator.platform import (
    BASELINE_END, SERVICES, STANDARD_METRICS, WINDOW, fmtTime, minutesAgo, timeAt,
)
from datetime import timedelta

UNITS={
    "requests_per_min": "req/min", "error_rate": "ratio (0-1)", "latency_p50_ms": "ms", "latency_p95_ms": "ms",
    "checkout_success_rate": "ratio (0-1)", "pricing_calls_per_request": "calls/request", "instance_count": "instances",
    "provider_requests_per_min": "req/min", "provider_error_rate": "ratio (0-1)",
    "duplicate_charges_per_min": "charges/min", "auth_failures_per_min": "failures/min",
    "charges_missing_idempotency_key_per_min": "charges/min",
}
CALL_GRAPH=("checkout-web -> checkout-api -> {pricing-service, inventory-service, payment-service -> Fabrikam Payments (external), order-service}")


def rnd(v):
    if v is None:
        return None
    a=abs(v)
    if a<1:
        return round(v, 4)
    if a<100:
        return round(v, 2)
    return round(v)


def mean(xs):
    return sum(xs)/len(xs) if xs else 0.0


def std(xs):
    m=mean(xs)
    return (sum((x-m)**2 for x in xs)/len(xs))**0.5 if xs else 0.0


def detectChange(values):
    """First minute after the baseline where 3 consecutive points leave the normal band."""
    base=values[:BASELINE_END]
    m, s=mean(base), std(base)
    band=max(4*s, 0.15*abs(m), 1e-6)
    for i in range(BASELINE_END, WINDOW-2):
        window=values[i:i+3]
        if all(v>m+band for v in window) or all(v<m-band for v in window):
            return {"startedAt": fmtTime(i), "minutesAgo": minutesAgo(i), "direction": "up" if window[0]>m else "down"}
    return None


def getServiceHealth(t):
    services=[]
    for name, meta in SERVICES.items():
        row={"service": name, "role": meta["role"]}
        status="healthy"
        for metric in ("requests_per_min", "error_rate", "latency_p95_ms"):
            values=t.series(name, metric)
            base, now=mean(values[:BASELINE_END]), mean(values[-5:])
            row[metric]={"baseline": rnd(base), "last5min": rnd(now)}
        err, p95=row["error_rate"], row["latency_p95_ms"]
        if err["last5min"]>max(3*err["baseline"], 0.01) or p95["last5min"]>2*p95["baseline"]:
            status="degraded"
        row["status"]=status
        services.append(row)
    extras={}
    for name, metrics in t.metrics.items():
        for metric in metrics:
            if metric not in STANDARD_METRICS:
                extras.setdefault(name, []).append(metric)
    return {"asOf": fmtTime(WINDOW-1), "callGraph": CALL_GRAPH, "services": services,
            "otherMetricsAvailable": extras,
            "note": "baseline = mean of the first hour of the 2-hour window; last5min = mean of the last 5 minutes"}


def queryMetrics(t, service, metric, minutes=60):
    if service not in SERVICES:
        return {"error": f"unknown service '{service}'", "services": list(SERVICES)}
    values=t.series(service, metric)
    if values is None:
        return {"error": f"service '{service}' has no metric '{metric}'", "availableMetrics": list(t.metrics.get(service, {}))}
    minutes=max(10, min(WINDOW, int(minutes)))
    start=WINDOW-minutes
    buckets=[]
    for i in range(start, WINDOW, 5):
        chunk=values[i:i+5]
        buckets.append({"t": fmtTime(i), "value": rnd(mean(chunk))})
    base=values[:BASELINE_END]
    latest=mean(values[-3:])
    baseMean=mean(base)
    change=((latest-baseMean)/baseMean*100) if baseMean else None
    return {"service": service, "metric": metric, "unit": UNITS.get(metric, ""),
            "baseline": rnd(baseMean), "baselineStd": rnd(std(base)), "latest": rnd(latest),
            "changePct": rnd(change) if change is not None else "n/a (baseline is 0)",
            "changeDetected": detectChange(values), "bucketMinutes": 5, "buckets": buckets}


def normalizePattern(message):
    msg=re.sub(r"ord_\w+", "<orderId>", message)
    msg=re.sub(r"SKU-\d+", "SKU-<n>", msg)
    msg=re.sub(r"\$\d+(\.\d+)?", "$<amount>", msg)
    msg=re.sub(r"\b\d+(\.\d+)?\b", "<n>", msg)
    return msg


def searchLogs(t, query, service=None, level=None, minutes=60, limit=12):
    terms=[w for w in query.lower().split() if w]
    if not terms:
        return {"error": "query must contain at least one word"}
    if service and service not in SERVICES:
        return {"error": f"unknown service '{service}'", "services": list(SERVICES)}
    minutes=max(5, min(WINDOW, int(minutes)))
    limit=max(1, min(20, int(limit)))
    fromIndex=WINDOW-minutes
    matches=[]
    for line in t.logs:
        if line.index<fromIndex:
            continue
        if service and line.service!=service:
            continue
        if level and line.level.lower()!=level.lower():
            continue
        hay=f"{line.service} {line.level} {line.message}".lower()
        if all(term in hay for term in terms):
            matches.append(line)
    if not matches:
        return {"query": query, "totalMatches": 0, "hint": "No matches. All terms must appear in a line; try fewer or different terms, another service, or a longer window."}
    patterns=Counter(normalizePattern(l.message) for l in matches)
    byService=Counter(l.service for l in matches)
    byLevel=Counter(l.level for l in matches)
    # samples: newest line of each distinct pattern first, then fill with newest lines
    samples, seen=[], set()
    for line in reversed(matches):
        p=normalizePattern(line.message)
        if p not in seen:
            seen.add(p)
            samples.append(line)
        if len(samples)>=limit:
            break
    if len(samples)<limit:
        for line in reversed(matches):
            if line not in samples:
                samples.append(line)
            if len(samples)>=limit:
                break
    samples.sort(key=lambda l: l.index)
    return {
        "query": query, "window": f"last {minutes} min", "totalMatches": len(matches),
        "firstSeen": fmtTime(matches[0].index), "lastSeen": fmtTime(matches[-1].index),
        "byService": dict(byService), "byLevel": dict(byLevel),
        "topPatterns": [{"pattern": p, "count": c} for p, c in patterns.most_common(6)],
        "samples": [{"t": fmtTime(l.index), "service": l.service, "level": l.level, "message": l.message} for l in samples],
    }


def getTraces(t, service, minDurationMs=0, status=None, limit=3):
    if service not in SERVICES:
        return {"error": f"unknown service '{service}'", "services": list(SERVICES)}
    limit=max(1, min(5, int(limit)))
    found=[tr for tr in t.traces
           if (tr.rootService==service or any(sp.service==service for sp in tr.spans))
           and tr.durationMs>=minDurationMs and (not status or tr.status==status)]
    found.sort(key=lambda tr: -tr.durationMs)
    if not found:
        return {"service": service, "traces": [], "hint": "No traces match. Lower minDurationMs or drop the status filter."}
    return {"service": service, "matched": len(found), "traces": [{
        "traceId": tr.traceId, "t": fmtTime(tr.index), "root": f"{tr.rootService} {tr.rootOperation}",
        "durationMs": tr.durationMs, "status": tr.status,
        "spans": [{"service": sp.service, "operation": sp.operation, "durationMs": sp.durationMs, "status": sp.status,
                   **({"note": sp.note} if sp.note else {})} for sp in tr.spans],
    } for tr in found[:limit]]}


def listDeploys(t, service=None, hours=24):
    hours=max(1, min(72, int(hours)))
    rows=[]
    for d in t.deploys:
        if d.minutesAgo>hours*60:
            continue
        if service and d.service!=service:
            continue
        deployedAt=(timeAt(WINDOW-1)-timedelta(minutes=d.minutesAgo)).strftime("%Y-%m-%dT%H:%MZ")
        rows.append({"deployId": d.deployId, "service": d.service, "version": d.version, "deployedAt": deployedAt,
                     "minutesAgo": d.minutesAgo, "author": d.author, "summary": d.summary, "status": d.status})
    return {"window": f"last {hours} h", "now": fmtTime(WINDOW-1), "deploys": rows}


def getDeployDiff(t, deployId):
    for d in t.deploys:
        if d.deployId==deployId:
            return {"deployId": d.deployId, "service": d.service, "version": d.version, "summary": d.summary, "diff": d.diff}
    return {"error": f"unknown deploy '{deployId}'", "knownDeploys": [d.deployId for d in t.deploys]}


def getServiceConfig(t, service):
    if service not in t.configs:
        return {"error": f"unknown service '{service}'", "services": list(t.configs)}
    return {"service": service, "config": copy.deepcopy(t.configs[service])}
