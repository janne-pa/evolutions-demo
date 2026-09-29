// Public defaults. For private overrides, copy to demo.local.bicepparam and pass
// -ParametersFile to scripts\deploy.ps1; local parameter files are excluded from Git.
using './main.bicep'

// Check model/version availability and quota in the chosen region before deployment.
param location = 'swedencentral'
// Set AZURE_PRINCIPAL_ID to the Entra object ID of the user who will run the local demo.
// Reading the environment keeps a personal identifier out of this public parameter file.
param principalId = readEnvironmentVariable('AZURE_PRINCIPAL_ID')
param resourceGroupName = 'rg-evolutions-demo'
// Chat generates answers; the separate judge evaluates those original answers.
param chatModel = 'gpt-4.1-mini'
param judgeModel = 'gpt-4.1'
// resources.bicep disables automatic model-version upgrades for reproducible configuration.
param chatVersion = '2025-04-14'
param judgeVersion = '2025-04-14'
param deploymentSku = 'DataZoneStandard'
// Applied separately to chat and judge. Capacity units and quota depend on the model/SKU.
param capacity = 20
