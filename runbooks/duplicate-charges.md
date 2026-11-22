# Runbook: duplicate charges

## Symptoms
Reconciliation job reports orders with two charges, billing support tickets ("charged twice").

## Why it happens
A client retries a payment after a timeout. If the retry carries the same Idempotency-Key, payment-service returns the first result. If the key is missing, payment-service treats the retry as a new charge. Slow provider responses cause more timeouts and therefore more retries.

## Diagnosis
- Check charges_missing_idempotency_key_per_min on payment-service and when it started.
- Search payment-service logs for "missing Idempotency-Key" and checkout-web logs for retries.
- Look for a recent deploy of a client (checkout-web, checkout-api) that changed how payments are submitted.

## Mitigation
- Stop new duplicates first: roll back the client change, or ship a hotfix that sends the Idempotency-Key again.
- Then refund the duplicate charges found by reconciliation.

## Follow-ups
Make Idempotency-Key required in payment-service (reject requests without it), add a contract test, make retries reuse the same key.
