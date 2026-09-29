// Subscription entry point: create a dedicated resource group, then deploy its contents.
// The caller needs resource-creation and role-assignment permissions in the subscription.
targetScope = 'subscription'

param location string
// Entra object ID of the local demo user; this is not an application/client ID.
param principalId string
param resourceGroupName string = 'rg-evolutions-demo'
param chatModel string
param judgeModel string
param chatVersion string
param judgeVersion string
param deploymentSku string
// Capacity applies to each model deployment, not a shared pool; units depend on model/SKU.
@minValue(1)
param capacity int

// Keep the demo isolated from other projects and mark its data as synthetic.
resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    project: 'evolutions-demo'
    data: 'synthetic-only'
  }
}

// Resource-group scope keeps the resource definitions separate from subscription setup.
module resources 'resources.bicep' = {
  name: 'evolutions-demo-resources'
  scope: group
  params: {
    location: location
    principalId: principalId
    chatModel: chatModel
    judgeModel: judgeModel
    chatVersion: chatVersion
    judgeVersion: judgeVersion
    deploymentSku: deploymentSku
    capacity: capacity
  }
}

// configure.ps1 reads these outputs from the named subscription deployment to build .env.
output projectEndpoint string = resources.outputs.projectEndpoint
output workspaceId string = resources.outputs.workspaceId
output appInsightsName string = resources.outputs.appInsightsName
output foundryName string = resources.outputs.foundryName
output resourceGroupName string = group.name
