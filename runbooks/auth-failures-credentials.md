# Runbook: authorization failures and credentials

## Symptoms
401 Unauthorized or 403 Forbidden from a provider or internal API, near-100% failure with low latency (requests fail fast).

## Diagnosis
- Search logs for 401/403 and for credential expiry warnings.
- Check the service config for secret metadata: source, expiresAt, rotation.
- Secrets stored in plain config files with manual rotation are the most common cause of expiry incidents.

## Mitigation
- Rotate the credential and reload the service. This restores traffic immediately.
- If a secret was ever printed in logs, treat it as leaked: rotate it again after the incident and purge the logs.

## Long-term fix
Store secrets in Azure Key Vault with automatic rotation, or remove the secret entirely by using managed identity (Microsoft Entra ID). This is part of Microsoft's Secure Future Initiative: no long-lived secrets in config. Alert on credential expiry at least 7 days ahead.
