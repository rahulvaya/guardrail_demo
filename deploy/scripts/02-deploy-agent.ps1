#requires -Version 7.0
<#
.SYNOPSIS
    Builds the agent image in ACR and deploys it to AKS with Workload Identity + Azure OpenAI.
#>

[CmdletBinding()]
param(
    [string] $ImageTag = (Get-Date -Format 'yyyyMMddHHmmss')
)

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$deployRoot = Join-Path $repoRoot 'deploy'
$stateFile  = Join-Path $deployRoot '.deploy-state.json'
$tokenFile  = Join-Path $deployRoot '.guardrails-token'
$manifest   = Join-Path $deployRoot 'k8s\agent.yaml'
$buildCtx   = Join-Path $repoRoot 'bankbuddy'
$dockerfile = Join-Path $buildCtx 'services\agent\Dockerfile'

if (-not (Test-Path $stateFile)) { throw "Run 00-deploy-infra.ps1 first - $stateFile missing." }
if (-not (Test-Path $tokenFile)) { throw "Run 01-deploy-guardrails.ps1 first - $tokenFile missing." }
$state = Get-Content $stateFile -Raw | ConvertFrom-Json
$internalToken = (Get-Content $tokenFile -Raw).Trim()

az account set --subscription $state.subscriptionId | Out-Null

$imageRepo = 'bankbuddy/agent'
$fullImage = "$($state.acrLoginServer)/$($imageRepo):$ImageTag"

Write-Host "==> Building $fullImage via ACR (context: $buildCtx)" -ForegroundColor Cyan
az acr build `
    --registry $state.acrName `
    --image "$($imageRepo):$ImageTag" `
    --file $dockerfile `
    $buildCtx `
    --only-show-errors --no-logs | Out-Host
if ($LASTEXITCODE -ne 0) { throw "ACR build failed (exit $LASTEXITCODE)" }

Write-Host "==> Ensuring guardrails-secrets exists in namespace" -ForegroundColor Cyan
kubectl create secret generic guardrails-secrets `
    --namespace bankbuddy `
    --from-literal=internal-token="$internalToken" `
    --dry-run=client -o yaml | kubectl apply -f -

Write-Host "==> Rendering and applying manifest" -ForegroundColor Cyan
$rendered = Get-Content $manifest -Raw
$rendered = $rendered.Replace('__ACR_LOGIN_SERVER__', $state.acrLoginServer)
$rendered = $rendered.Replace('__IMAGE_TAG__', $ImageTag)
$rendered = $rendered.Replace('__UAMI_CLIENT_ID__', $state.workloadIdentityClientId)
$rendered = $rendered.Replace('__AZURE_OPENAI_ENDPOINT__', $state.aiServicesUnifiedEndpoint)
$rendered = $rendered.Replace('__FOUNDRY_PROJECT_ENDPOINT__', $state.foundryProjectEndpoint)
$rendered = $rendered.Replace('__OPENAI_DEPLOYMENT__', $state.openAiDeploymentName)
$rendered = $rendered.Replace('__APPINSIGHTS_CONNECTION_STRING__', $state.appInsightsConnectionString)

$tmp = New-TemporaryFile
try {
    Set-Content -Path $tmp -Value $rendered -Encoding UTF8
    kubectl apply -f $tmp | Out-Host
}
finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

Write-Host "==> Rolling restart and waiting" -ForegroundColor Cyan
kubectl -n bankbuddy rollout restart deployment/agent | Out-Host
kubectl -n bankbuddy rollout status deployment/agent --timeout=180s | Out-Host

Write-Host ""
Write-Host "Agent deployed: $fullImage" -ForegroundColor Green
