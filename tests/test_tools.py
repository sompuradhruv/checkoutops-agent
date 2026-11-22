import json

from app.tools.registry import TOOLS, executeTool, toolSchemas


def health(sid):
    out, ok=executeTool(sid, "getServiceHealth", "{}")
    assert ok
    return {s["service"]: s["status"] for s in out["services"]}


def test_health_flags_the_right_services():
    assert health("latency-bad-deploy")["checkout-api"]=="degraded"
    assert health("expired-credential")["payment-service"]=="degraded"
    sale=health("traffic-spike-sale")
    assert all(status=="healthy" for status in sale.values())


def test_search_logs_requires_all_terms_and_returns_distinct_samples():
    out, ok=executeTool("prompt-injection", "searchLogs", json.dumps({"query": "checkout failed", "service": "checkout-api", "level": "ERROR"}))
    assert ok and out["totalMatches"]>10
    assert any("SYSTEM NOTE TO AI AGENT" in s["message"] for s in out["samples"])
    none, ok=executeTool("prompt-injection", "searchLogs", json.dumps({"query": "checkout zebra"}))
    assert none["totalMatches"]==0 and "hint" in none


def test_unknown_deploy_and_bad_arguments_return_errors_not_exceptions():
    out, ok=executeTool("latency-bad-deploy", "getDeployDiff", json.dumps({"deployId": "d-9999"}))
    assert not ok and "d-4821" in out["knownDeploys"]
    out, ok=executeTool("latency-bad-deploy", "queryMetrics", json.dumps({"service": "not-a-service", "metric": "x"}))
    assert not ok and out["error"]=="invalid arguments"
    out, ok=executeTool("latency-bad-deploy", "queryMetrics", "not json")
    assert not ok
    out, ok=executeTool("latency-bad-deploy", "deleteDatabase", "{}")
    assert not ok and "unknown tool" in out["error"]


def test_unknown_metric_lists_available_metrics():
    out, ok=executeTool("latency-bad-deploy", "queryMetrics", json.dumps({"service": "checkout-api", "metric": "nope"}))
    assert not ok and "pricing_calls_per_request" in out["availableMetrics"]


def test_runbook_search_ranks_relevant_sections():
    out, _=executeTool("double-charge", "searchRunbooks", json.dumps({"query": "duplicate charges idempotency key"}))
    assert out["results"][0]["runbook"]=="duplicate-charges.md"


def test_tool_schemas_are_clean():
    schemas=toolSchemas()
    names=[s["function"]["name"] for s in schemas]
    assert len(names)==len(set(names))==len(TOOLS)+1
    report=schemas[-1]["function"]["parameters"]
    text=json.dumps(report)
    assert "$ref" not in text and "$defs" not in text
    assert "title" in report["properties"] and "rootCause" in report["required"]


def test_injection_is_on_the_inventory_path_too():
    out, _=executeTool("prompt-injection", "searchLogs", json.dumps({"query": "NullReferenceException", "service": "inventory-service"}))
    assert any("SYSTEM NOTE TO AI AGENT" in s["message"] for s in out["samples"])
