// Orchestrator VM: AutoGen orchestrator, validation pipeline runner, L1 sandbox,
// OpenTelemetry collector.
//
// D2s_v3 rather than the D4s_v3 in Task 7 §4.4.0 — the region quota is 10 vCPU
// total and AKS needs four of them. Burstable B-series is deliberately avoided
// despite being cheaper: CPU credit throttling would add variance to exactly
// the latency measurements RQ2 depends on.

param location string
param namePrefix string
param tags object
param subnetId string

@description('VM size. Constrained by the regional vCPU quota.')
param vmSize string = 'Standard_D2s_v3'

param adminUsername string = 'afadmin'

@description('SSH public key. Password authentication is disabled.')
param sshPublicKey string

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${namePrefix}-vm-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: { id: subnetId }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: '${namePrefix}-vm'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }   // no secrets on disk
  properties: {
    hardwareProfile: { vmSize: vmSize }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: { storageAccountType: 'Premium_LRS' }
        diskSizeGB: 64
      }
    }
    osProfile: {
      computerName: '${namePrefix}-vm'
      adminUsername: adminUsername
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
        patchSettings: { patchMode: 'AutomaticByPlatform' }
      }
    }
    networkProfile: {
      networkInterfaces: [{ id: nic.id }]
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
  }
}

output vmName string = vm.name
output vmPrincipalId string = vm.identity.principalId
output vmPrivateIp string = nic.properties.ipConfigurations[0].properties.privateIPAddress
