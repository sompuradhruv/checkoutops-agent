# Runbook: payment provider errors (Fabrikam Payments)

## Symptoms
payment-service errors, 5xx (503, 502, 504) or timeouts from Fabrikam Payments, checkout success rate dropping.

## Diagnosis
- Search payment-service logs for responses from Fabrikam Payments and for the provider status check.
- Compare provider_requests_per_min with the normal rate. A large increase with flat customer traffic means a retry storm.
- Check payment-service config: retryPolicy (attempts, backoff), circuitBreaker.enabled, secondaryProvider.failoverEnabled.
- Confirm no recent payment-service deploy explains it. Provider outages are not fixed by rollbacks.

## Mitigation
- Fail over card processing to the secondary provider (Northwind Pay) when the primary is down. Medium risk: watch authorization rates.
- Enable the circuit breaker to stop hammering the failing provider and fail fast.
- Restore exponential backoff with jitter on retries; never retry immediately.

## Follow-ups
Keep failover enabled, alert on provider error rate, test failover regularly.
