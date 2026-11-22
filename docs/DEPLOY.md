# Deploying to Azure

Target setup: Azure OpenAI (in Microsoft Foundry) for the model, Azure Container Apps for the app, and a **managed identity** between them, so the hosted app holds no API key.

Run these from the repo root in a terminal with the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed (Azure Cloud Shell works too).

## 0. One-time setup

```bash
az login
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.CognitiveServices

RG=rg-checkoutops
LOC=eastus2              # pick a region where your model is available
AOAI=checkoutops-aoai-$RANDOM   # must be globally unique; note the value
APP=checkoutops-agent
ENV=checkoutops-env

az group create --name $RG --location $LOC
```

## 1. Azure OpenAI resource and model deployment

Easiest in the portal: go to [ai.azure.com](https://ai.azure.com), create a resource in `$RG`, open **Models + endpoints**, deploy **gpt-5-mini** (deployment type Global Standard) and name the deployment `gpt-5-mini`. Optionally deploy **gpt-5** too, for escalation and the model comparison in the evals.

Or with the CLI:

```bash
az cognitiveservices account create --name $AOAI --resource-group $RG --location $LOC \
  --kind OpenAI --sku S0 --custom-domain $AOAI

# find the current version string for the model
az cognitiveservices account list-models --name $AOAI --resource-group $RG \
  --query "[?name=='gpt-5-mini'].{name:name, version:version}" -o table

az cognitiveservices account deployment create --name $AOAI --resource-group $RG \
  --deployment-name gpt-5-mini --model-name gpt-5-mini --model-version <VERSION> \
  --model-format OpenAI --sku-name GlobalStandard --sku-capacity 50
```

Get the endpoint (and a key for local use only):

```bash
ENDPOINT=$(az cognitiveservices account show -n $AOAI -g $RG --query properties.endpoint -o tsv)
az cognitiveservices account keys list -n $AOAI -g $RG --query key1 -o tsv
```

## 2. Test locally, run evals, record replays

```bash
cp .env.example .env    # set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AGENT_MODEL=gpt-5-mini
set -a; source .env; set +a

uvicorn app.main:app --reload                                   # try it at http://localhost:8000
python -m evals.run_evals --models gpt-5-mini,gpt-5 --repeats 3 # fills the Eval results tab
python -m scripts.record_replays --only-passing                 # fallback recordings for the demo
```

Commit `replays/*.json` and `evals/results/latest.json`. They ship inside the image. `.env` is git-ignored: never commit it.

## 3. Deploy the app

```bash
az containerapp up --name $APP --resource-group $RG --location $LOC --environment $ENV \
  --source . --ingress external --target-port 8000
```

This builds the Dockerfile in Azure Container Registry and creates the Container Apps environment and app.

## 4. Managed identity instead of a key

```bash
az containerapp identity assign --name $APP --resource-group $RG --system-assigned
PRINCIPAL=$(az containerapp show -n $APP -g $RG --query identity.principalId -o tsv)
AOAI_ID=$(az cognitiveservices account show -n $AOAI -g $RG --query id -o tsv)

az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope $AOAI_ID
```

## 5. Configure and scale

```bash
az containerapp update --name $APP --resource-group $RG \
  --min-replicas 0 --max-replicas 1 \
  --set-env-vars LLM_PROVIDER=azure AZURE_OPENAI_ENDPOINT=$ENDPOINT AZURE_USE_MANAGED_IDENTITY=true \
    AGENT_MODEL=gpt-5-mini LLM_REASONING_EFFORT=low DAILY_BUDGET_USD=2 LIVE_RUNS_PER_IP_PER_HOUR=5

az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv
```

Notes:
- Role assignments can take a few minutes to apply. Early 401/403 errors usually clear by themselves.
- `--max-replicas 1` keeps the in-memory rate limit and budget correct.
- `--min-replicas 0` scales to zero when idle (a few seconds of cold start). In the week of the interview, set `--min-replicas 1` so the page is instant.
- To add escalation: deploy `gpt-5` and add `ESCALATION_MODEL=gpt-5`.

## 6. Cost guardrails

- The app stops live runs at `DAILY_BUDGET_USD` and serves recordings after that.
- Also set an Azure budget alert: portal, **Cost Management**, **Budgets**, for example $10/month on `$RG`.
- The container registry created by `up` has a small monthly cost even when idle.

## Redeploy after changes

```bash
az containerapp up --name $APP --resource-group $RG --environment $ENV --source .
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DeploymentNotFound` / 404 | `AGENT_MODEL` must equal the deployment name in Foundry, not the model family. |
| 401 / 403 from Azure OpenAI | Wait for the role assignment; check the identity is enabled; the resource needs a custom subdomain. |
| Runs always play recordings | `/api/config` shows `liveConfigured`; check env vars and today's budget. |
| See server logs | `az containerapp logs show -n $APP -g $RG --follow` |

## Tear down

```bash
az group delete --name $RG --yes
```
