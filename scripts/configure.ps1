<#
.SYNOPSIS
Create the local .env file from an existing evolutions-demo subscription deployment.
.DESCRIPTION
Run after deploy.ps1. This reads that deployment's outputs, not arbitrary existing
Azure resources. Existing .env files are deliberately preserved.
.PARAMETER SubscriptionId
Subscription containing the deployment and its Application Insights resource.
.EXAMPLE
.\scripts\configure.ps1 -SubscriptionId "<subscription-id>"
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$SubscriptionId)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
# Never overwrite a working environment or a user's manual configuration.
if (Test-Path "$root\.env") { throw '.env already exists. Preserve or remove it explicitly first.' }
# Output names are the contract defined in infra\main.bicep.
$raw = az deployment sub show --subscription $SubscriptionId --name evolutions-demo `
    --query properties.outputs -o json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Cannot read deployment outputs.' }
$outputs = $raw | ConvertFrom-Json
# The connection string selects the ingestion destination; an Entra token authorizes export.
# Fetch it here rather than printing it as a deployment output.
$resourceId = "/subscriptions/$SubscriptionId/resourceGroups/$($outputs.resourceGroupName.value)/providers/Microsoft.Insights/components/$($outputs.appInsightsName.value)"
$connection = az resource show --ids $resourceId --api-version 2020-02-02 `
    --query properties.ConnectionString -o tsv --only-show-errors
if ($LASTEXITCODE -ne 0 -or !$connection) { throw 'Cannot read telemetry connection string.' }
$tenant = az account show --subscription $SubscriptionId --query tenantId -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Cannot read tenant.' }
# Deployment names match resources.bicep. AzureCliCredential avoids unrelated cached logins.
# Keep this generated file local: .gitignore excludes it, and no values are printed below.
@"
AZURE_AI_PROJECT_ENDPOINT=$($outputs.projectEndpoint.value)
CHAT_DEPLOYMENT=apple-chat
JUDGE_DEPLOYMENT=apple-judge
APPLICATIONINSIGHTS_CONNECTION_STRING=$connection
LOG_ANALYTICS_WORKSPACE_ID=$($outputs.workspaceId.value)
AZURE_TENANT_ID=$tenant
AZURE_TOKEN_CREDENTIALS=AzureCliCredential
"@ | Set-Content -LiteralPath "$root\.env" -Encoding utf8
Write-Output 'Local .env created from deployment outputs. No model keys used.'
