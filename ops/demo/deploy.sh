#!/usr/bin/env bash
# Deploy the demo backend to Azure Container Apps (express), with its identity.
#
#   bash ops/demo/deploy.sh
#
# Needs: az logged in with rights on the resource group, kubectl with the
# agentfac-aks context, and the cluster running (to mint the service-account
# token). Re-running updates what exists.
#
# Why express, and what that changes: in every region this subscription's
# policy allows, new Container Apps environments are express-only. Express is
# consumption-billed with scale to zero and the same free grant, but has no
# system-assigned identities, no Key Vault secret references and no jobs. So
# the app uses a user-assigned identity, reads the model key from Key Vault
# itself, and the idle-cluster auto-stop runs as a scheduled GitHub Actions
# workflow that logs in with the same identity through OIDC (no stored secret).
set -euo pipefail
export MSYS_NO_PATHCONV=1

SUB=1c063792-23ea-4e59-ac80-7fed220d9711
RG=rg-agentfactory-dev
LOC=centralindia
ENV=agentfac-demo-env
APP=agentfac-demo
ID=agentfac-demo-id
KV=agentfac-kv
REPO=bob-dev-01/multiagent-sandbox
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

echo "== identity and its roles"
if ! az identity show -g $RG -n $ID >/dev/null 2>&1; then
  az identity create -g $RG -n $ID -l $LOC -o none
fi
ID_RESOURCE=$(az identity show -g $RG -n $ID --query id -o tsv)
ID_CLIENT=$(az identity show -g $RG -n $ID --query clientId -o tsv)
ID_PRINCIPAL=$(az identity show -g $RG -n $ID --query principalId -o tsv)
for role_scope in "Agent Factory demo operator|$RG_ID" "Cost Management Reader|$RG_ID" \
    "Key Vault Secrets User|$(az keyvault show -n $KV --query id -o tsv)"; do
  az role assignment create --assignee-object-id "$ID_PRINCIPAL" --assignee-principal-type ServicePrincipal \
    --role "${role_scope%%|*}" --scope "${role_scope#*|}" -o none 2>/dev/null || true
done
# The auto-stop workflow logs in as this identity, from main only.
if ! az identity federated-credential show -g $RG --identity-name $ID -n github-main >/dev/null 2>&1; then
  az identity federated-credential create -g $RG --identity-name $ID -n github-main \
    --issuer https://token.actions.githubusercontent.com \
    --subject "repo:bob-dev-01@132093634/multiagent-sandbox@1375667911:ref:refs/heads/main" --audiences api://AzureADTokenExchange -o none
fi

echo "== environment (express)"
if ! az containerapp env show -g $RG -n $ENV >/dev/null 2>&1; then
  az containerapp env create -g $RG -n $ENV -l $LOC --logs-destination none -o none
fi

echo "== app"
if ! az containerapp show -g $RG -n $APP >/dev/null 2>&1; then
  az containerapp create -g $RG -n $APP --environment $ENV --image $IMAGE \
    --ingress external --target-port 8080 --min-replicas 0 --max-replicas 1 \
    --cpu 2 --memory 4Gi --user-assigned "$ID_RESOURCE" -o none
fi

echo "== secrets and settings"
# The model key is not copied into the app: the app reads it from Key Vault
# with its identity (agentfactory.secrets) when it first needs it.
az containerapp secret set -g $RG -n $APP -o none --secrets \
  "access-code=$ACCESS_CODE" "k8s-token=$K8S_TOKEN" "k8s-ca=$K8S_CA_B64"
az containerapp update -g $RG -n $APP --image $IMAGE -o none \
  --set-env-vars "AZURE_CLIENT_ID=$ID_CLIENT" "AZURE_KEY_VAULT_NAME=$KV" \
    "DEMO_ACCESS_CODE=secretref:access-code" \
    "K8S_TOKEN=secretref:k8s-token" "K8S_CA_B64=secretref:k8s-ca" "K8S_SERVER=$K8S_SERVER" \
    "DEMO_ALLOWED_ORIGINS=$ORIGIN,http://localhost:8000" "DEMO_DAILY_BUDGET_USD=3" \
    "AGENTFACTORY_POD_CPU_REQUEST=0.1" \
    "AZURE_SUBSCRIPTION_ID=$SUB" "AZURE_RESOURCE_GROUP=$RG" "AGENTFACTORY_ENV=demo"

FQDN=$(az containerapp show -g $RG -n $APP --query properties.configuration.ingress.fqdn -o tsv)
echo "== backend: https://$FQDN"
echo "== identity client id for the auto-stop workflow: $ID_CLIENT"
printf '// Where the live backend runs. Written by ops/demo/deploy.sh.\nwindow.AF_BACKEND = "https://%s";\n' "$FQDN" > demo/web/config.js
