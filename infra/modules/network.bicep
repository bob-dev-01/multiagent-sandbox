// Network isolation — the architecture's primary containment boundary.
//
// Two subnets, two NSGs, and they are not the same rule. Task 7 states the
// egress policy three different ways (§4.4.0, §4.4.3, §6.4) and only §6.4 has
// it right; this template implements §6.4 (see OQ-1 in architecture.md).
//
//   orchestrator subnet — may reach the Anthropic API, Blob Storage and GitHub.
//                         Holds the credentials. Never runs generated code.
//   sandbox subnet      — DENY ALL egress. No exceptions, including Anthropic.
//                         Runs generated code. Holds no credentials.
//
// The sandbox subnet is delegated to Azure Container Instances, which is what
// makes the NSG apply to container groups at all: an ACI group deployed with a
// public IP instead of into a delegated subnet is not behind this NSG, and the
// containment claim would silently not hold.

@description('Azure region for all network resources.')
param location string

@description('Prefix for resource names.')
param namePrefix string

@description('Tags applied to every resource.')
param tags object

var vnetName = '${namePrefix}-vnet'
var orchestratorSubnetName = 'snet-orchestrator'
var sandboxSubnetName = 'snet-sandbox'
var aksSubnetName = 'snet-aks'

resource orchestratorNsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${namePrefix}-nsg-orchestrator'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowAnthropicApiOutbound'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '443'
          description: 'Model and embedding calls. Orchestrator only.'
        }
      }
      {
        name: 'AllowAzureStorageOutbound'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Storage'
          destinationPortRange: '443'
          description: 'JSONL log and corpus upload.'
        }
      }
      {
        name: 'AllowKeyVaultOutbound'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureKeyVault'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowSqlOutbound'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Sql'
          destinationPortRange: '5432'
          description: 'Agent Registry.'
        }
      }
      {
        name: 'DenyAllOtherOutbound'
        properties: {
          priority: 4096
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource sandboxNsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${namePrefix}-nsg-sandbox'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        // The whole point of this subnet. If a rule is ever added above this
        // one, the isolation claim in the thesis stops being true.
        name: 'DenyAllOutbound'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
          description: 'Sandboxed agents have no network. No exceptions.'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource aksNsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${namePrefix}-nsg-aks'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        // AKS nodes need egress to reach the managed control plane and pull
        // images. Pod-level egress for sandbox workloads is denied separately
        // by a Kubernetes NetworkPolicy, not here — an AKS node subnet with no
        // egress cannot join its own cluster.
        name: 'AllowAksControlPlaneOutbound'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureCloud'
          destinationPortRange: '443'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.42.0.0/16']
    }
    subnets: [
      {
        name: orchestratorSubnetName
        properties: {
          addressPrefix: '10.42.1.0/24'
          networkSecurityGroup: { id: orchestratorNsg.id }
          serviceEndpoints: [
            { service: 'Microsoft.Storage' }
            { service: 'Microsoft.KeyVault' }
          ]
        }
      }
      {
        name: sandboxSubnetName
        properties: {
          addressPrefix: '10.42.2.0/24'
          networkSecurityGroup: { id: sandboxNsg.id }
          delegations: [
            {
              name: 'aci-delegation'
              properties: { serviceName: 'Microsoft.ContainerInstance/containerGroups' }
            }
          ]
        }
      }
      {
        name: aksSubnetName
        properties: {
          addressPrefix: '10.42.4.0/22'
          networkSecurityGroup: { id: aksNsg.id }
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output orchestratorSubnetId string = '${vnet.id}/subnets/${orchestratorSubnetName}'
output sandboxSubnetId string = '${vnet.id}/subnets/${sandboxSubnetName}'
output aksSubnetId string = '${vnet.id}/subnets/${aksSubnetName}'
