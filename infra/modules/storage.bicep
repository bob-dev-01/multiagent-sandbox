// Blob storage: JSONL log store, agent corpus archive, SHA-256 manifests.
// GRS because the logs are the experiment — losing them loses the study, and
// they are far cheaper to replicate than to regenerate.

param location string
param namePrefix string
param tags object

@description('Subnet allowed to reach storage. The sandbox subnet is deliberately not in this list.')
param allowedSubnetId string

var storageName = toLower(replace('${namePrefix}st', '-', ''))

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: length(storageName) > 24 ? substring(storageName, 0, 24) : storageName
  location: location
  tags: tags
  sku: { name: 'Standard_GRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false   // Managed Identity only; no keys to leak
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      virtualNetworkRules: [
        { id: allowedSubnetId, action: 'Allow' }
      ]
    }
    encryption: {
      services: {
        blob: { enabled: true, keyType: 'Account' }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
    isVersioningEnabled: true
  }
}

resource logsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'validation-logs'
  properties: { publicAccess: 'None' }
}

resource corpusContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'agent-corpus'
  properties: { publicAccess: 'None' }
}

output storageAccountName string = storage.name
output storageAccountId string = storage.id
