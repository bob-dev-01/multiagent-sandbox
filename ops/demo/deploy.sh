#!/usr/bin/env bash
# Deploy the demo backend to Azure Container Apps, and the auto-stop job.
#
#   bash ops/demo/deploy.sh
#
# Needs: az logged in with rights on the resource group, kubectl with the
# agentfac-aks context, and the cluster running (to mint the service-account
# token). Re-running updates what exists. Everything stays in the free tiers:
# a consumption-only environment with no log workspace, scale to zero, one
# replica at most.
set -euo pipefail
export MSYS_NO_PATHCONV=1

SUB=1c063792-23ea-4e59-ac80-7fed220d9711
RG=rg-agentfactory-dev
LOC=centralindia
ENV=agentfac-demo-env
APP=agentfac-demo
JOB=agentfac-demo-autostop
KV=agentfac-kv
IMAGE=ghcr.io/bob-dev-01/agentfactory-demo:latest
ORIGIN=https://bob-dev-01.github.io
RG_ID=/subscriptions/$SUB/resourceGroups/$RG

az account set --subscription "$SUB"

echo "== cluster access for the demo"
kubectl --context agentfac-aks apply -f ops/demo/k8s-demo-access.yaml >/dev/null
K8S_TOKEN=$(kubectl --context agentfac-aks -n agentfactory-sandbox get secret demo-runner-token -o jsonpath='{.data.token}' | base64 -d)
K8S_CA_B64=$(kubectl --context agentfac-aks -n agentfactory-sandbox get secret demo-runner-token -o jsonpath='{.data.ca\.crt}')
K8S_SERVER="https://$(az aks show -g $RG -n agentfac-aks --query fqdn -o tsv):443"
ACCESS_CODE=$(az keyvault secret show --vault-name $KV -n demo-access-code --query value -o tsv)

echo "== environment"
if ! az containerapp env show -g $RG -n $ENV >/dev/null 2>&1; then
  az containerapp env create -g $RG -n $ENV -l $LOC --logs-destination none -o none
fi

echo "== app"
if ! az containerapp show -g $RG -n $APP >/dev/null 2>&1; then
  az containerapp create -g $RG -n $APP --environment $ENV --image $IMAGE \
    --ingress external --target-port 8080 --min-replicas 0 --max-replicas 1 \
    --cpu 2 --memory 4Gi --system-assigned -o none
fi
PRINCIPAL=$(az containerapp show -g $RG -n $APP --query identity.principalId -o tsv)

echo "== roles for the app's identity"
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Agent Factory demo operator" --scope "$RG_ID" -o none 2>/dev/null || true
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Cost Management Reader" --scope "$RG_ID" -o none 2>/dev/null || true
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope "$(az keyvault show -n $KV --query id -o tsv)" -o none 2>/dev/null || true

# The model key is not copied into the app: the app reads it from Key Vault
# with its managed identity (agentfactory.secrets) when it first needs it.
echo "== secrets and settings"
az containerapp secret set -g $RG -n $APP -o none --secrets \
  "access-code=$ACCESS_CODE" "k8s-token=$K8S_TOKEN" "k8s-ca=$K8S_CA_B64"
az containerapp update -g $RG -n $APP --image $IMAGE -o none \
  --set-env-vars "AZURE_KEY_VAULT_NAME=$KV" "DEMO_ACCESS_CODE=secretref:access-code" \
    "K8S_TOKEN=secretref:k8s-token" "K8S_CA_B64=secretref:k8s-ca" "K8S_SERVER=$K8S_SERVER" \
    "DEMO_ALLOWED_ORIGINS=$ORIGIN,http://localhost:8000" "DEMO_DAILY_BUDGET_USD=3" \
    "AZURE_SUBSCRIPTION_ID=$SUB" "AZURE_RESOURCE_GROUP=$RG" "AGENTFACTORY_ENV=demo"

echo "== auto-stop job (every 20 minutes; stops the cluster after 40 idle minutes)"
if ! az containerapp job show -g $RG -n $JOB >/dev/null 2>&1; then
  az containerapp job create -g $RG -n $JOB --environment $ENV --trigger-type Schedule \
    --cron-expression "*/20 * * * *" --replica-timeout 300 --replica-retry-limit 0 \
    --image $IMAGE --cpu 0.5 --memory 1Gi --system-assigned \
    --command "python" --args "-m" "agentfactory.demo.autostop" -o none
fi
JOB_PRINCIPAL=$(az containerapp job show -g $RG -n $JOB --query identity.principalId -o tsv)
az role assignment create --assignee-object-id "$JOB_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Agent Factory demo operator" --scope "$RG_ID" -o none 2>/dev/null || true
az containerapp job update -g $RG -n $JOB --image $IMAGE -o none \
  --set-env-vars "AZURE_SUBSCRIPTION_ID=$SUB" "AZURE_RESOURCE_GROUP=$RG" "DEMO_IDLE_MINUTES=40"

FQDN=$(az containerapp show -g $RG -n $APP --query properties.configuration.ingress.fqdn -o tsv)
echo "== backend: https://$FQDN"
printf '// Where the live backend runs. Written by ops/demo/deploy.sh.\nwindow.AF_BACKEND = "https://%s";\n' "$FQDN" > demo/web/config.js
