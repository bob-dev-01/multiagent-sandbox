// Agent Registry — PostgreSQL Flexible Server with pgvector.
//
// Burstable B1ms: the registry holds hundreds of rows, not millions, and the
// tier can be stopped from the control panel, which matters more than
// throughput on a metered subscription.

param location string
param namePrefix string
param tags object

@secure()
@description('Administrator password. Supplied at deploy time, never stored in the repo.')
param administratorPassword string

param administratorLogin string = 'afadmin'

@description('Allow access from Azure services. Required for the orchestrator VM.')
param allowAzureServices bool = true

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${namePrefix}-pg'
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'agentfactory'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// pgvector must be allowlisted at the server level before CREATE EXTENSION works.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR,UUID-OSSP'
    source: 'user-override'
  }
}

resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (allowAzureServices) {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [extensions]
}

output postgresName string = postgres.name
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output databaseName string = database.name
