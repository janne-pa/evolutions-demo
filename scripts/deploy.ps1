<#
.SYNOPSIS
Deploy the demo's Bicep infrastructure to an explicitly selected subscription.
.DESCRIPTION
Requires an Azure CLI login, Bicep, model quota and permissions to create resources
and role assignments. Set AZURE_PRINCIPAL_ID before using the public parameter file.
.PARAMETER SubscriptionId
Target subscription ID; the script does not rely on the CLI's current default.
.PARAMETER ParametersFile
Bicep parameter file. Use infra\demo.local.bicepparam for private overrides.
.EXAMPLE
.\scripts\deploy.ps1 -SubscriptionId "<subscription-id>"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [string]$ParametersFile = "$PSScriptRoot\..\infra\demo.bicepparam"
)
$ErrorActionPreference = 'Stop'
# This location stores subscription deployment metadata; resource locations come from Bicep.
# Keep the deployment name aligned with configure.ps1, which reads its outputs.
az deployment sub create --subscription $SubscriptionId --location swedencentral `
    --name evolutions-demo --parameters $ParametersFile --only-show-errors
# ErrorActionPreference alone does not reliably turn native CLI exit codes into exceptions.
if ($LASTEXITCODE -ne 0) { throw 'Bicep deployment failed; inspect Azure deployment operations.' }
