# Runbook: checkout latency above SLO

## Symptoms
p95 latency on checkout-web or checkout-api above the 1,000 ms SLO, upstream timeouts in checkout-web, falling checkout success rate.

## Diagnosis
- Compare the latency change start time with recent deploys of checkout-api and its dependencies.
- Pull slow traces. Many sequential calls to the same downstream service inside one request indicate an N+1 pattern (one call per cart item instead of one batch call).
- Check pricing_calls_per_request and pricing-service request rate: a jump means more calls per checkout, not more customers.
- Check whether the dependency itself got slower (its own p95) or whether it is just being called more often.

## Mitigation
- If a deploy correlates with the regression and touches the slow path: roll it back first. Rollback is low risk and fast.
- Do not roll back unrelated deploys (for example cosmetic frontend changes).
- If no deploy is involved and traffic is high, scale out checkout-api.

## Follow-ups
Batch downstream calls, add a latency gate between rollout rings, add a performance test for large carts.
