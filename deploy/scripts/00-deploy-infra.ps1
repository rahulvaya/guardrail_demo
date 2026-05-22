#requires -Version 7.0
<#
.SYNOPSIS
    Deploys the BankBuddy AKS base infrastructure + AI services + workload identity.

.DESCRIPTION
    Sets the subscription, creates the resource group, deploys main.bicep,
    fetches AKS credentials, and writes a state file consumed by the other
    deploy scripts.

    Idempotent - safe to re-run when the Bicep changes.

.PARAMETER SubscriptionId
    Azure subscription ID. Defaults to the value from the conversation context.

.PARAMETER ResourceGroup
    Resource group name. Will be created if it doesn't exist.

.PARAMETER Location
    Azure region.

.EXAMPLE
    ./00-deploy-infra.ps1
#>

[CmdletBinding()]
param(
    [string] $SubscriptionId = 'c2f434b3-f6dd-402a-b106-b3071428d122',
    [string] $ResourceGroup  = 'rg-bankbuddy-aks',
    [string] $Location       = 'eastus',
    [string] $DeploymentName = "bankbuddy-infra-$(Get-Date -Format yyyyMMddHHmmss)"
)

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$deployRoot = Join-Path $repoRoot 'deploy'
$bicepFile  = Join-Path $deployRoot 'bicep\main.bicep'
$paramsFile = Join-Path $deployRoot 'bicep\main.parameters.json'
$stateFile  = Join-Path $deployRoot '.deploy-state.json'

Write-Host "==> Setting subscription $SubscriptionId" -ForegroundColor Cyan
az account set --subscription $SubscriptionId | Out-Null

Write-Host "==> Ensuring resource group '$ResourceGroup' in '$Location'" -ForegroundColor Cyan
az group create --name $ResourceGroup --location $Location --only-show-errors | Out-Null

Write-Host "==> Deploying Bicep (deployment: $DeploymentName)" -ForegroundColor Cyan
$deployJson = az deployment group create `
    --resource-group $ResourceGroup `
    --name $DeploymentName `
    --template-file $bicepFile `
    --parameters "@$paramsFile" `
    --only-show-errors `
    -o json
if ($LASTEXITCODE -ne 0) { throw "Bicep deployment failed (exit $LASTEXITCODE)" }

$deploy  = $deployJson | ConvertFrom-Json
$outputs = $deploy.properties.outputs

$state = [ordered]@{
    subscriptionId          = $SubscriptionId
    resourceGroup           = $ResourceGroup
    location                = $Location
    aksName                 = $outputs.aksName.value
    acrName                 = $outputs.acrName.value
    acrLoginServer          = $outputs.acrLoginServer.value
    workloadIdentityName    = $outputs.workloadIdentityName.value
    workloadIdentityClientId= $outputs.workloadIdentityClientId.value
    aksOidcIssuerUrl        = $outputs.aksOidcIssuerUrl.value
    aiServicesName          = $outputs.aiServicesName.value
    aiServicesEndpoint      = $outputs.aiServicesEndpoint.value
    aiServicesOpenAiEndpoint= $outputs.aiServicesOpenAiEndpoint.value
    aiServicesUnifiedEndpoint = $outputs.aiServicesUnifiedEndpoint.value
    contentSafetyName       = $outputs.contentSafetyName.value
    contentSafetyEndpoint   = $outputs.contentSafetyEndpoint.value
    foundryProjectName      = $outputs.foundryProjectName.value
    foundryProjectEndpoint  = $outputs.foundryProjectEndpoint.value
    openAiDeploymentName    = $outputs.openAiDeploymentName.value
}
$state | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding UTF8
Write-Host "==> Wrote state file: $stateFile" -ForegroundColor Green

Write-Host "==> Fetching AKS credentials" -ForegroundColor Cyan
az aks get-credentials `
    --resource-group $ResourceGroup `
    --name $state.aksName `
    --overwrite-existing `
    --only-show-errors | Out-Null

Write-Host "==> Ensuring namespace 'bankbuddy'" -ForegroundColor Cyan
kubectl apply -f (Join-Path $deployRoot 'k8s\namespace.yaml') | Out-Host

Write-Host ""
Write-Host "Infra ready:" -ForegroundColor Green
$state | Format-List
