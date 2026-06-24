#requires -Version 7.0
<#
.SYNOPSIS
    Builds the guardrails image in ACR and deploys it to AKS with Workload Identity.
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
$manifest   = Join-Path $deployRoot 'k8s\guardrails.yaml'
$svcRoot    = Join-Path $repoRoot 'guardrails-service'
$dockerfile = Join-Path $svcRoot 'Dockerfile'

if (-not (Test-Path $stateFile)) { throw "Run 00-deploy-infra.ps1 first - $stateFile missing." }
$state = Get-Content $stateFile -Raw | ConvertFrom-Json

az account set --subscription $state.subscriptionId | Out-Null

$imageRepo = 'bankbuddy/guardrails'
$fullImage = "$($state.acrLoginServer)/$($imageRepo):$ImageTag"

Write-Host "==> Building $fullImage via ACR (context: $svcRoot)" -ForegroundColor Cyan
az acr build `
    --registry $state.acrName `
    --image "$($imageRepo):$ImageTag" `
    --file $dockerfile `
    $svcRoot `
    --only-show-errors --no-logs | Out-Host
if ($LASTEXITCODE -ne 0) { throw "ACR build failed (exit $LASTEXITCODE)" }

# Internal token shared by guardrails + agent.
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -Path $tokenFile -NoNewline -Encoding ASCII
    Write-Host "==> Generated new internal token at $tokenFile" -ForegroundColor Green
}
$internalToken = (Get-Content $tokenFile -Raw).Trim()

Write-Host "==> Applying namespace" -ForegroundColor Cyan
kubectl apply -f (Join-Path $deployRoot 'k8s\namespace.yaml') | Out-Host

Write-Host "==> Applying guardrails-secrets" -ForegroundColor Cyan
kubectl create secret generic guardrails-secrets `
    --namespace bankbuddy `
    --from-literal=internal-token="$internalToken" `
    --dry-run=client -o yaml | kubectl apply -f -

Write-Host "==> Rendering and applying manifest" -ForegroundColor Cyan
$rendered = Get-Content $manifest -Raw
$rendered = $rendered.Replace('__ACR_LOGIN_SERVER__', $state.acrLoginServer)
$rendered = $rendered.Replace('__IMAGE_TAG__', $ImageTag)
$rendered = $rendered.Replace('__UAMI_CLIENT_ID__', $state.workloadIdentityClientId)
$rendered = $rendered.Replace('__AI_SERVICES_ENDPOINT__', $state.aiServicesEndpoint)
$rendered = $rendered.Replace('__CONTENT_SAFETY_ENDPOINT__', $state.contentSafetyEndpoint)
$rendered = $rendered.Replace('__APPINSIGHTS_CONNECTION_STRING__', $state.appInsightsConnectionString)

$tmp = New-TemporaryFile
try {
    Set-Content -Path $tmp -Value $rendered -Encoding UTF8
    kubectl apply -f $tmp | Out-Host
}
finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

Write-Host "==> Rolling restart (pick up new image) and waiting" -ForegroundColor Cyan
kubectl -n bankbuddy rollout restart deployment/guardrails | Out-Host
kubectl -n bankbuddy rollout status deployment/guardrails --timeout=180s | Out-Host

Write-Host ""
Write-Host "Guardrails deployed: $fullImage" -ForegroundColor Green
