// Resource-group module: telemetry, Foundry models/project and runtime RBAC.
// FastAPI and the evaluation CLI stay local; this template does not host the application.
param location string
param principalId string
param chatModel string
param judgeModel string
param chatVersion string
param judgeVersion string
param deploymentSku string
param capacity int

// Stable names let repeated deployments target the same resources in this resource group.
var suffix = uniqueString(resourceGroup().id)
// Built-in role IDs are assigned at individual resource scopes below, not subscription scope.
var foundryUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
var openaiUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
var logsReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
var metricsPublisherRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')

// Evaluation reads original turn payloads from AppTraces in this workspace.
// This retention policy does not delete local reports or Foundry evaluation records.
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-evolutions-${suffix}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// Link Application Insights to the queryable workspace and require Entra-authenticated export.
// Keep all telemetry here; sampling away turn records would leave gaps in evaluation input.
resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-evolutions-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
    RetentionInDays: 30
    SamplingPercentage: 100
  }
}

// Local clients use public endpoints with Entra tokens; model-key authentication is disabled.
// This is a demo topology, not a private-endpoint/VNet deployment.
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-evolutions-${suffix}'
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'ai-evolutions-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

// The project identity is used for service-side evaluation and receives model access below.
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: 'evolutions-demo'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Evolutions apple demo'
    description: 'Local apple chat and original-response dataset evaluations. Synthetic data only.'
  }
  // Serialize account child writes: chat -> judge -> project, avoiding RequestConflict errors.
  dependsOn: [
    judge
  ]
}

// Separate deployments prevent the answering model from also being its own judge.
// Both deployments pin model versions; upgrades must be an explicit parameter change.
resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'apple-chat'
  sku: {
    name: deploymentSku
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModel
      version: chatVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource judge 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'apple-judge'
  sku: {
    name: deploymentSku
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: judgeModel
      version: judgeVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  // Do not deploy sibling models concurrently on the same Foundry account.
  dependsOn: [
    chat
  ]
}

// The local user manages evaluator definitions and evaluation runs in this project.
resource projectUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, principalId, foundryUserRole)
  scope: project
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: foundryUserRole
  }
}

// Local model calls need account-level model access in addition to project access.
resource modelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, openaiUserRole)
  scope: account
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: openaiUserRole
  }
}

// Cloud-side judging uses the project's managed identity, not the local user's credential.
resource projectModelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, project.id, openaiUserRole)
  scope: account
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: openaiUserRole
  }
}

// The evaluation CLI queries AppTraces with the local user's Azure CLI credential.
resource logReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workspace.id, principalId, logsReaderRole)
  scope: workspace
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: logsReaderRole
  }
}

// Authorize OpenTelemetry ingestion while Application Insights local authentication is off.
resource telemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, principalId, metricsPublisherRole)
  scope: insights
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: metricsPublisherRole
  }
}

// Return endpoint/resource identifiers, not model keys. The workspace ID is its customer GUID,
// not the ARM resource ID; LogsQueryClient requires that GUID when querying the workspace.
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output workspaceId string = workspace.properties.customerId
output appInsightsName string = insights.name
output foundryName string = account.name
