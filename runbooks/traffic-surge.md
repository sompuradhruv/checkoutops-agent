# Runbook: traffic surge

## Symptoms
Request rate far above forecast on checkout services.

## Diagnosis
- Check for marketing campaigns, sales and feature flags (for example a homepage banner). Campaign traffic carries a utm_campaign tag.
- Check the health signals, not just the volume: error rate, p95 latency against the SLO, checkout success rate.
- Check autoscaling: instance_count rising and keeping up is the system working as designed.

## Decision
- If error rate and latency are within SLO and autoscaling keeps up: this is not an incident. Severity none. Take no action other than monitoring, and tune the alert to use the campaign forecast.
- Only scale out manually if latency approaches the SLO or autoscaling has hit its maximum.
- Never roll back deploys or fail over providers because of healthy load.
