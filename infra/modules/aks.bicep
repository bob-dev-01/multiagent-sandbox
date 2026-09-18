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

@description('''Sandbox pool node size. B-series v2 is the only family with quota, capacity
and AKS support on this subscription, so the L3 latency measurements sit on burstable hardware —
record CPU credit balance alongside them (see vm.bicep for the full reasoning).''')
param nodeSize string = 'Standard_B2s_v2'

@description('''System pool node size. Kept as its own parameter so the system pool can move
off the sandbox pool's family if quota ever allows a non-burstable sandbox node.''')
param systemNodeSize string = 'Standard_B2s_v2'

@description('Maximum nodes per pool. Held at 1 by the regional vCPU quota.')
param maxNodesPerPool int = 1

@description('''Kubernetes version. Pinned so an upgrade cannot silently break the gVisor
DaemonSet. 1.35 is the regional default and carries standard support; 1.32 and below are
LTS-only on this subscription and are rejected at preflight.''')
param kubernetesVersion string = '1.35'

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
        maxCount: maxNodesPerPool
        enableAutoScaling: true
        vmSize: systemNodeSize
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
        maxCount: maxNodesPerPool
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
