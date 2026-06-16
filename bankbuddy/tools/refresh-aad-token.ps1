#requires -Version 5.1
<#
.SYNOPSIS
    Fetch an Entra ID bearer token for Azure OpenAI using the signed-in user
    and recreate the agent container so it picks the token up.

.DESCRIPTION
    Use this when the BankBuddy service principal is blocked by a Conditional
    Access policy and you cannot enable API key auth on the Azure OpenAI
    resource. Your *user* identity satisfies CA (you can sign into the
    portal), so we acquire a token as you and hand it to the container via
    the AZURE_OPENAI_AAD_TOKEN env var.

    Tokens last ~60-75 minutes. Re-run this script when /chat starts
    returning 401 / 502.

.PREREQUISITES
    1. Azure CLI installed (`az --version`).
    2. `az login --tenant 16b3c013-d300-468d-ac64-7eda0820b6d3` once.
    3. Your user has the "Cognitive Services OpenAI User" role on the
       v-shs-mnwsbnrd-eastus2 resource (most devs already do).

.EXAMPLE
    .\tools\refresh-aad-token.ps1
#>

[CmdletBinding()]
param(
    [string]$Tenant = '16b3c013-d300-468d-ac64-7eda0820b6d3',
    [string]$Resource = 'https://cognitiveservices.azure.com',
    # Separate audience for the azure-ai-evaluation SDK (safety + quality evaluators).
    # The SDK internally requests tokens with this scope via get_token_provider().
    [string]$EvalResource = 'https://ai.azure.com',
    [switch]$NoRecreate
)

$ErrorActionPreference = 'Stop'

# Ensure az is available
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI not found. Install from https://aka.ms/InstallAzureCli."
}

# Ensure logged in to the right tenant
$account = az account show --output json 2>$null | ConvertFrom-Json
if (-not $account -or $account.tenantId -ne $Tenant) {
    Write-Host "Signing in to tenant $Tenant ..." -ForegroundColor Yellow
    az login --tenant $Tenant --output none
    if ($LASTEXITCODE -ne 0) { throw "az login failed" }
}

Write-Host "Acquiring token for $Resource ..." -ForegroundColor Cyan
$tokenJson = az account get-access-token --resource $Resource --output json
if ($LASTEXITCODE -ne 0) { throw "az account get-access-token failed" }
$token = ($tokenJson | ConvertFrom-Json).accessToken
if (-not $token) { throw "Empty token returned" }

# Export to current shell so docker compose substitutes it
$env:AZURE_OPENAI_AAD_TOKEN = $token
# Same token works for Azure AI Content Safety (same audience / scope).
$env:AZURE_CONTENT_SAFETY_AAD_TOKEN = $token
# Same token works for Azure AI Language (PII Entity Recognition).
$env:AZURE_LANGUAGE_AAD_TOKEN = $token
Write-Host "AZURE_OPENAI_AAD_TOKEN set (length=$($token.Length))." -ForegroundColor Green
Write-Host "AZURE_CONTENT_SAFETY_AAD_TOKEN set (length=$($token.Length))." -ForegroundColor Green
Write-Host "AZURE_LANGUAGE_AAD_TOKEN set (length=$($token.Length))." -ForegroundColor Green

# Fetch a second token scoped to ai.azure.com for the azure-ai-evaluation SDK.
# The SDK's get_token_provider calls credential.get_token("https://ai.azure.com/.default"),
# so passing a cognitiveservices.azure.com token would fail with audience mismatch.
Write-Host "Acquiring evaluation token for $EvalResource ..." -ForegroundColor Cyan
$evalTokenJson = az account get-access-token --resource $EvalResource --output json
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not fetch ai.azure.com token — evaluation quality/safety evaluators may fail."
} else {
    $evalToken = ($evalTokenJson | ConvertFrom-Json).accessToken
    $env:EVAL_AZURE_AAD_TOKEN = $evalToken
    Write-Host "EVAL_AZURE_AAD_TOKEN set (length=$($evalToken.Length))." -ForegroundColor Green
}

if ($NoRecreate) {
    Write-Host "Skipping container recreate (-NoRecreate)." -ForegroundColor Yellow
    return
}

# Recreate the guardrails and agent containers so they inherit the new tokens.
# Guardrails calls Azure AI Content Safety / Language; agent calls Azure
# OpenAI via LiteLLM using AZURE_OPENAI_AAD_TOKEN.
Push-Location (Join-Path $PSScriptRoot '..')
try {
    Write-Host "Recreating guardrails and agent containers ..." -ForegroundColor Cyan
    docker compose up -d --force-recreate guardrails agent | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
    Write-Host "Guardrails and agent restarted with fresh AAD token." -ForegroundColor Green
} finally {
    Pop-Location
}
