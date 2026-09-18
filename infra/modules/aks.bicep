// AKS — substrate for the L3 gVisor sandbox.
//
// Two node pools. The system pool runs cluster services only. The sandbox pool
// is where generated code executes: it is tainted so nothing schedules there by
// accident, labelled so the L3 runner's nodeSelector can find it, and it is the
// pool that gets runsc installed onto it by the DaemonSet in
// infra/scripts/install-gvisor-daemonset.yaml.
//
// Both pools can scale to zero from the control panel. That is the difference
// between an idle cluster costing nothing and costing a node-hour every hour.
//
// The Free tier control plane carries no uptime SLA, which is the correct
// trade for a research cluster.

param location string
param namePrefix string
param tags object
param subnetId string
param logAnalyticsWorkspaceId string

@description('Node size for both pools. Constrained by the regional vCPU quota.')
param nodeSize string = 'Standard_D2s_v3'

@description('Kubernetes version. Pinned so an upgrade cannot silently break the gVisor DaemonSet.')
param kubernetesVersion string = '1.32'

resource aks 'Microsoft.ContainerService/managedClusters@2024-09-01' = {
  name: '${namePrefix}-aks'
  location: location
  tags: tags
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: { type: 'SystemAssigned' }
  properties: {
    dnsPrefix: '${namePrefix}-aks'
    kubernetesVersion: kubernetesVersion
    enableRBAC: true
    disableLocalAccounts: false
    networkProfile: {
      networkPlugin: 'azure'
      networkPolicy: 'calico'   // pod-level egress denial for the sandbox namespace
      serviceCidr: '10.43.0.0/16'
      dnsServiceIP: '10.43.0.10'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: 1
        minCount: 1
        maxCount: 2
        enableAutoScaling: true
        vmSize: nodeSize
        osType: 'Linux'
        osSKU: 'Ubuntu'
        osDiskSizeGB: 64
        vnetSubnetID: subnetId
        maxPods: 30
        type: 'VirtualMachineScaleSets'
      }
      {
        name: 'sandbox'
        mode: 'User'
        count: 1
        minCount: 0          // scale to zero when idle
        maxCount: 2
        enableAutoScaling: true
        vmSize: nodeSize
        osType: 'Linux'
        osSKU: 'Ubuntu'
        osDiskSizeGB: 64
        vnetSubnetID: subnetId
        maxPods: 30
        type: 'VirtualMachineScaleSets'
        nodeLabels: {
          'agentfactory.io/sandbox': 'gvisor'
        }
        nodeTaints: [
          // Nothing lands here unless it explicitly tolerates the taint.
          'agentfactory.io/sandbox=gvisor:NoSchedule'
        ]
      }
    ]
    addonProfiles: {
      omsagent: {
        enabled: true
        config: { logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceId }
      }
    }
    autoUpgradeProfile: {
      // Manual: an unattended node-image upgrade would replace the nodes the
      // gVisor DaemonSet configured, silently reverting L3 to plain runc.
      upgradeChannel: 'none'
    }
  }
}

output aksName string = aks.name
output aksId string = aks.id
output aksPrincipalId string = aks.identity.principalId
output nodeResourceGroup string = aks.properties.nodeResourceGroup
