// Orchestrator VM: AutoGen orchestrator, validation pipeline runner, L1 sandbox,
// OpenTelemetry collector.
//
// B2s_v2, not the D4s_v3 of Task 7 §4.4.0. This is a forced choice, not a
// preference, and it has a measurement consequence worth stating plainly.
//
// On this Azure for Students subscription: the regional allowance is 6 vCPU;
// no v5 or v6 family has any quota at all; and every D-family that does have
// quota (DSv3, DSv4, DDv4...) is either capacity-restricted for VMs or refused
// outright by AKS. The B-series v2 families are the only ones with quota,
// capacity and AKS support simultaneously.
//
// B-series is burstable, so sustained load exhausts CPU credits and the host
// throttles. The L1 sandbox runs on this VM, so a long batch can produce
// latency that rises over time for reasons that have nothing to do with the
// isolation level — a confound that would otherwise look like an RQ2 finding.
// The mitigation is to record the CPU credit balance alongside every latency
// sample so the effect is visible in the data rather than silently mixed into
// it; see docs/limitations in the README.

param location string
param namePrefix string
param tags object
param subnetId string

@description('VM size. Constrained by the regional vCPU quota.')
param vmSize string = 'Standard_B2s_v2'

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
