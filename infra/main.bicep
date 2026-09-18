// Agent Factory — full infrastructure, one deployment.
//
// Subscription-scoped so it creates its own resource group: this is meant to go
// into a dedicated subscription, and everything it makes should be removable by
// deleting one group.
//
// Deploy:
//   az deployment sub create \
//     --location polandcentral \
//     --template-file infra/main.bicep \
//     --parameters infra/params/dev.bicepparam
//
// Tear down (the control panel wraps this):
//   az group delete --name rg-agentfactory-dev --yes

targetScope = 'subscription'

@description('Region for every resource.')
param location string = 'polandcentral'

@description('Resource group to create.')
param resourceGroupName string = 'rg-agentfactory-dev'

@description('Prefix for resource names. Keep it short — storage names cap at 24 characters.')
@minLength(3)
@maxLength(12)
param namePrefix string = 'agentfac'

@description('Environment label, used in tags and for the budget name.')
param environment string = 'dev'

@secure()
@description('PostgreSQL administrator password.')
param postgresAdminPassword string

@description('SSH public key for the orchestrator VM.')
param sshPublicKey string

@description('Object ID of the deploying user, granted Key Vault secret write access.')
param adminPrincipalId string = ''

@description('Monthly budget in USD. Alerts fire at 50%, 80% actual and 100% forecast.')
param monthlyBudgetUsd int = 150

@description('Where budget alerts go.')
param budgetContactEmail string

@description('Budget period start, first of a month, YYYY-MM-DD.')
param budgetStartDate string

@description('Deploy AKS. Set false for a cheaper L1/L2-only environment.')
param deployAks bool = true

var tags = {
  project: 'agentfactory'
  environment: environment
  owner: budgetContactEmail
  purpose: 'masters-research'
  // Tagged explicitly because this environment executes untrusted generated
  // code and should never be mistaken for a general-purpose one.
  workload: 'untrusted-code-execution'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module network 'modules/network.bicep' = {
  scope: rg
  name: 'network'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
  }
}

module observability 'modules/observability.bicep' = {
  scope: rg
  name: 'observability'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storage'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    allowedSubnetId: network.outputs.orchestratorSubnetId
  }
}

module postgres 'modules/postgres.bicep' = {
  scope: rg
  name: 'postgres'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    administratorPassword: postgresAdminPassword
  }
}

module vm 'modules/vm.bicep' = {
  scope: rg
  name: 'vm'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    subnetId: network.outputs.orchestratorSubnetId
    sshPublicKey: sshPublicKey
  }
}

module keyvault 'modules/keyvault.bicep' = {
  scope: rg
  name: 'keyvault'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    readerPrincipalId: vm.outputs.vmPrincipalId
    adminPrincipalId: adminPrincipalId
  }
}

module aks 'modules/aks.bicep' = if (deployAks) {
  scope: rg
  name: 'aks'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
    subnetId: network.outputs.aksSubnetId
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
  }
}

module budget 'modules/budget.bicep' = {
  name: 'budget'
  params: {
    budgetName: '${namePrefix}-${environment}-monthly'
    amountUsd: monthlyBudgetUsd
    contactEmail: budgetContactEmail
    startDate: budgetStartDate
  }
}

// Storage Blob Data Contributor, so the VM writes logs with its managed
// identity rather than an account key (the account has key auth disabled).
var blobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

module storageRole 'modules/role-assignment.bicep' = {
  scope: rg
  name: 'vm-storage-role'
  params: {
    principalId: vm.outputs.vmPrincipalId
    roleDefinitionId: blobContributorRoleId
    storageAccountName: storage.outputs.storageAccountName
  }
}

output resourceGroupName string = rg.name
output location string = location
output vmName string = vm.outputs.vmName
output vmPrivateIp string = vm.outputs.vmPrivateIp
output keyVaultName string = keyvault.outputs.keyVaultName
output storageAccountName string = storage.outputs.storageAccountName
output postgresFqdn string = postgres.outputs.postgresFqdn
output databaseName string = postgres.outputs.databaseName
output sandboxSubnetId string = network.outputs.sandboxSubnetId
output aksName string = aks.?outputs.aksName ?? ''
output appInsightsConnectionString string = observability.outputs.appInsightsConnectionString
