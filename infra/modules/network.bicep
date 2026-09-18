// Network isolation — the architecture's primary containment boundary.
//
// Two subnets, two NSGs, and they are not the same rule. Task 7 states the
// egress policy three different ways (§4.4.0, §4.4.3, §6.4) and only §6.4 has
// it right; this template implements §6.4 (see OQ-1 in architecture.md).
//
//   orchestrator subnet — may reach the Anthropic API, Blob Storage and GitHub.
//                         Holds the credentials. Never runs generated code.
//   sandbox subnet      — deny all egress except a container-registry pull.
//                         Runs generated code. Holds no credentials. The one
//                         exception is forced and is documented at the rule.
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
        // OS package repositories. Ubuntu archives answer on port 80, so a
        // 443-only allowlist silently leaves the VM unable to install anything
        // — and, more seriously, unable to receive the automatic security
        // patches the VM is configured to take. This is the orchestrator
        // subnet: it holds credentials but never runs generated code, so HTTP
        // egress here does not touch the sandbox containment story.
        name: 'AllowPackageRepositoriesOutbound'
        properties: {
          priority: 135
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '80'
          description: 'apt and OS patching. Orchestrator subnet only.'
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
        // Container image pull. This is a concession, and it is worth being
        // precise about what it costs.
        //
        // A VNet-injected ACI container group pulls its image *through this
        // subnet*, so a literal deny-all rule means the container never starts
        // — verified: ACI fails with RegistryErrorResponse from index.docker.io.
        // The NSG cannot tell "the platform is fetching an image" from "the
        // agent is calling out", because both are the same subnet egressing.
        //
        // So the allowance is made as narrow as it can be: 443 to Microsoft's
        // container registry service tags only, never the open internet and
        // never Docker Hub. What a compromised agent gains is the ability to
        // reach an anonymous, read-only Microsoft registry. That is a real
        // residual channel (DNS and timing at minimum) rather than none, and
        // it is recorded as such in architecture.md rather than glossed over.
        //
        // The clean fix is a private Azure Container Registry endpoint, which
        // keeps the pull entirely inside the VNet and needs no egress at all.
        // It requires ACR Premium, which is a fifth of this study's monthly
        // credit — so it is the documented production answer, not the one
        // deployed here.
        // One tag per rule: Azure accepts a service tag only in the singular
        // destinationAddressPrefix and rejects the plural array outright.
        name: 'AllowMcrPull'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'MicrosoftContainerRegistry'
          destinationPortRange: '443'
          description: 'Image pull only. Not a general egress allowance.'
        }
      }
      {
        // MCR serves layers through Front Door, so the registry tag alone is
        // not enough to complete a pull.
        name: 'AllowFrontDoorPull'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureFrontDoor.FirstParty'
          destinationPortRange: '443'
          description: 'Image layer delivery for MCR.'
        }
      }
      {
        // Everything else. If a rule is ever added above this one for any
        // reason other than image pull, the isolation claim stops being true.
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
          description: 'Sandboxed agents have no other network access.'
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
