# Incident response basics (checkout platform)

## Severity levels
- SEV1: payments broken or more than 25% of checkouts failing, or customers charged incorrectly. Page the payments on-call immediately.
- SEV2: major degradation: checkout success below 95%, latency SLO breached for most users, or a large customer segment blocked.
- SEV3: partial degradation with a workaround, small customer impact.
- SEV4: minor issue, no customer impact. Track in the backlog.
- none: the alert fired but nothing is broken (expected load, false positive). Tune the alert.

## Order of work
1. Assess customer impact: checkout success rate, error rate, how many orders affected.
2. Look for what changed: recent deploys, config changes, feature flags, dependency incidents, traffic.
3. Mitigate first: roll back, fail over, disable the flag. Restore service before fully understanding it.
4. Confirm the root cause with evidence from at least two sources (metrics, logs, traces, deploy diffs, config).
5. Write follow-ups: tests, alerts and guardrails so it cannot happen again.

## Correlation is not causation
A deploy that happened near the incident is a suspect, not a culprit. Check that the change touches the failing code path and that the timing matches within a few minutes. Unrelated deploys (other services, cosmetic changes) are common red herrings.
