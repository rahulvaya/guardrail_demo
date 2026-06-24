#requires -Version 7.0
<#
.SYNOPSIS
    Builds and deploys mock-bank + api + ui (plus in-cluster postgres) to AKS.

.DESCRIPTION
    Idempotent. Generates DB passwords and JWT secret on first run and persists
    them in deploy/.app-state.json. Exposes api and ui via LoadBalancer
    services; once both public IPs are assigned, re-renders PUBLIC_UI_ORIGIN
    and PUBLIC_API_BASE_URL and restarts the affected deployments.
#>

[CmdletBinding()]
param(
    [string] $ImageTag = (Get-Date -Format 'yyyyMMddHHmmss'),
    # Optional public DNS hostname (e.g. bankbuddy-demo.eastus.cloudapp.azure.com)
    # If set, the API CORS allow-list will include http://<hostname> in addition
    # to the UI LoadBalancer IP origin.
    [string] $PublicHostname = $env:BANKBUDDY_PUBLIC_HOSTNAME
)

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$deployRoot = Join-Path $repoRoot 'deploy'
$stateFile  = Join-Path $deployRoot '.deploy-state.json'
$appState   = Join-Path $deployRoot '.app-state.json'
$bankbuddy  = Join-Path $repoRoot 'bankbuddy'
$initSql    = Join-Path $bankbuddy 'infra\postgres\init.sql'

if (-not (Test-Path $stateFile)) { throw "Run 00-deploy-infra.ps1 first - $stateFile missing." }
$state = Get-Content $stateFile -Raw | ConvertFrom-Json
az account set --subscription $state.subscriptionId | Out-Null

# ---------------------------------------------------------------------------
# Generate / load app secrets (postgres passwords + JWT secret)
# ---------------------------------------------------------------------------
function New-RandomBase64($bytes = 24) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    [Convert]::ToBase64String($buf) -replace '[+/=]', 'a'
}

if (-not (Test-Path $appState)) {
    $secrets = @{
        postgresPassword = New-RandomBase64 24
        appPassword      = New-RandomBase64 24
        agentPassword    = New-RandomBase64 24
        bankPassword     = New-RandomBase64 24
        jwtSecret        = New-RandomBase64 32
    }
    $secrets | ConvertTo-Json | Set-Content -Path $appState -Encoding UTF8
    Write-Host "==> Generated new app secrets at $appState" -ForegroundColor Green
}
$secrets = Get-Content $appState -Raw | ConvertFrom-Json

# ---------------------------------------------------------------------------
# Build images in ACR (mock-bank, api, ui). Build context = bankbuddy/.
# ---------------------------------------------------------------------------
function Invoke-AcrBuild([string] $repo, [string] $dockerfile, [string] $context) {
    $fullImage = "$($state.acrLoginServer)/$($repo):$ImageTag"
    Write-Host "==> Building $fullImage" -ForegroundColor Cyan
    az acr build `
        --registry $state.acrName `
        --image "$($repo):$ImageTag" `
        --file $dockerfile `
        $context `
        --only-show-errors --no-logs | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "ACR build failed for $repo (exit $LASTEXITCODE)" }
    return $fullImage
}

$mockImage = Invoke-AcrBuild 'bankbuddy/mock-bank' (Join-Path $bankbuddy 'services\mock-bank\Dockerfile') $bankbuddy
$apiImage  = Invoke-AcrBuild 'bankbuddy/api'       (Join-Path $bankbuddy 'services\api\Dockerfile')       $bankbuddy
$uiImage   = Invoke-AcrBuild 'bankbuddy/ui'        (Join-Path $bankbuddy 'services\ui\Dockerfile')        $bankbuddy

# ---------------------------------------------------------------------------
# Ensure namespace + secrets + postgres init ConfigMap
# ---------------------------------------------------------------------------
Write-Host "==> Applying namespace" -ForegroundColor Cyan
kubectl apply -f (Join-Path $deployRoot 'k8s\namespace.yaml') | Out-Host

Write-Host "==> Applying postgres-secrets" -ForegroundColor Cyan
kubectl create secret generic postgres-secrets `
    --namespace bankbuddy `
    --from-literal=postgres-password="$($secrets.postgresPassword)" `
    --from-literal=app-password="$($secrets.appPassword)" `
    --from-literal=agent-password="$($secrets.agentPassword)" `
    --from-literal=bank-password="$($secrets.bankPassword)" `
    --dry-run=client -o yaml | kubectl apply -f -

Write-Host "==> Applying app-secrets" -ForegroundColor Cyan
kubectl create secret generic app-secrets `
    --namespace bankbuddy `
    --from-literal=jwt-secret="$($secrets.jwtSecret)" `
    --dry-run=client -o yaml | kubectl apply -f -

Write-Host "==> Applying postgres-init ConfigMap" -ForegroundColor Cyan
kubectl create configmap postgres-init `
    --namespace bankbuddy `
    --from-file=init.sql=$initSql `
    --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# Deploy postgres + wait
# ---------------------------------------------------------------------------
Write-Host "==> Deploying postgres" -ForegroundColor Cyan
kubectl apply -f (Join-Path $deployRoot 'k8s\postgres.yaml') | Out-Host
kubectl -n bankbuddy rollout status deployment/postgres --timeout=300s | Out-Host

# ---------------------------------------------------------------------------
# Render + apply mock-bank, api, ui manifests
# ---------------------------------------------------------------------------
function Invoke-RenderApply([string] $manifestPath) {
    $rendered = Get-Content $manifestPath -Raw
    $rendered = $rendered.Replace('__ACR_LOGIN_SERVER__', $state.acrLoginServer)
    $rendered = $rendered.Replace('__IMAGE_TAG__', $ImageTag)
    $rendered = $rendered.Replace('__APPINSIGHTS_CONNECTION_STRING__', $state.appInsightsConnectionString)
    $tmp = New-TemporaryFile
    try {
        Set-Content -Path $tmp -Value $rendered -Encoding UTF8
        kubectl apply -f $tmp | Out-Host
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "==> Deploying mock-bank" -ForegroundColor Cyan
Invoke-RenderApply (Join-Path $deployRoot 'k8s\mock-bank.yaml')
kubectl -n bankbuddy rollout restart deployment/mock-bank | Out-Host
kubectl -n bankbuddy rollout status  deployment/mock-bank --timeout=180s | Out-Host

Write-Host "==> Deploying api" -ForegroundColor Cyan
Invoke-RenderApply (Join-Path $deployRoot 'k8s\api.yaml')
kubectl -n bankbuddy rollout restart deployment/api | Out-Host

Write-Host "==> Deploying ui" -ForegroundColor Cyan
Invoke-RenderApply (Join-Path $deployRoot 'k8s\ui.yaml')
kubectl -n bankbuddy rollout restart deployment/ui | Out-Host

# ---------------------------------------------------------------------------
# Wait for LoadBalancer public IPs
# ---------------------------------------------------------------------------
function Wait-LoadBalancerIp([string] $svc, [int] $timeoutSec = 300) {
    Write-Host "==> Waiting for LoadBalancer IP on svc/$svc (up to ${timeoutSec}s)" -ForegroundColor Cyan
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $ip = kubectl -n bankbuddy get svc $svc -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
        if ($ip) { Write-Host "    -> $svc => $ip" -ForegroundColor Green; return $ip }
        Start-Sleep -Seconds 5
    }
    throw "svc/$svc did not receive a LoadBalancer IP within ${timeoutSec}s"
}

$apiIp = Wait-LoadBalancerIp 'api'
$uiIp  = Wait-LoadBalancerIp 'ui'

$apiUrl = "http://$apiIp"
$uiUrl  = "http://$uiIp"

# ---------------------------------------------------------------------------
# Patch CORS + PUBLIC_API_BASE_URL with the resolved public URLs, then
# wait for the rollouts triggered by `kubectl set env`.
# ---------------------------------------------------------------------------
Write-Host "==> Patching api PUBLIC_UI_ORIGIN=$uiUrl" -ForegroundColor Cyan
$uiOrigins = $uiUrl
if ($PublicHostname) {
    $uiOrigins = "$uiUrl,http://$PublicHostname,https://$PublicHostname"
    Write-Host "    (also allowing http(s)://$PublicHostname)" -ForegroundColor DarkGray
}
kubectl -n bankbuddy set env deployment/api PUBLIC_UI_ORIGIN="$uiOrigins" | Out-Host
kubectl -n bankbuddy rollout status deployment/api --timeout=180s | Out-Host

# Same-origin: the React app calls /api/* which the UI server reverse-proxies
# to API_INTERNAL_URL in-cluster. This keeps the session cookie SameSite=Lax-safe.
Write-Host "==> Patching ui PUBLIC_API_BASE_URL=/api (same-origin)" -ForegroundColor Cyan
kubectl -n bankbuddy set env deployment/ui PUBLIC_API_BASE_URL="/api" | Out-Host
kubectl -n bankbuddy rollout status deployment/ui --timeout=180s | Out-Host

# ---------------------------------------------------------------------------
# Persist URLs into state file and print summary.
# ---------------------------------------------------------------------------
$state | Add-Member -NotePropertyName apiPublicUrl -NotePropertyValue $apiUrl -Force
$state | Add-Member -NotePropertyName uiPublicUrl  -NotePropertyValue $uiUrl  -Force
$state | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding UTF8

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " BankBuddy app stack deployed" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  UI  (browser):  $uiUrl"
Write-Host "  API (browser):  $apiUrl"
Write-Host "  Images:"
Write-Host "    $mockImage"
Write-Host "    $apiImage"
Write-Host "    $uiImage"
Write-Host ""
Write-Host "Open $uiUrl in your browser."
