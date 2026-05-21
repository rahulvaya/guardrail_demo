# Azure Deployment - BankBuddy Agent + Guardrails on AKS

Deploys the standalone **guardrails-service** and the **bankbuddy agent** to a
single AKS cluster. Each workload has its own deploy script so they can be
released independently.

## Layout

```
deploy/
  bicep/
    main.bicep                # RG-scoped: Log Analytics, ACR, AKS (+ AcrPull)
    main.parameters.json
  k8s/
    namespace.yaml
    guardrails.yaml           # Deployment + ClusterIP Service + Secret
    agent.yaml                # Deployment + ClusterIP Service + Secret
  scripts/
    00-deploy-infra.ps1       # Creates RG + deploys Bicep
    01-deploy-guardrails.ps1  # Builds image -> ACR -> applies guardrails.yaml
    02-deploy-agent.ps1       # Builds image -> ACR -> applies agent.yaml
```

## Target

| Item            | Value                                    |
|-----------------|------------------------------------------|
| Subscription    | `c2f434b3-f6dd-402a-b106-b3071428d122`   |
| Resource Group  | `rg-bankbuddy-aks` (new)                 |
| Region          | `eastus` (override with `-Location`)     |
| AKS             | `aks-bankbuddy` (1 node, Standard_D2s_v3)|
| ACR             | `acrbankbuddy<suffix>` (Basic)           |
| Namespace       | `bankbuddy`                              |

Both services land on the same cluster + namespace; only an internal
`ClusterIP` Service is exposed for each (no public ingress yet — matches the
"internal network only" model documented in
`bankbuddy/docs/security-boundaries.md`).

## Prerequisites

- Azure CLI (`az`) logged in: `az login`
- `kubectl` and `docker` on PATH
- PowerShell 7+

## Deploy order

```powershell
cd deploy/scripts

# 1. Infra (RG, Log Analytics, ACR, AKS) - run once
./00-deploy-infra.ps1

# 2. Guardrails service
./01-deploy-guardrails.ps1

# 3. Agent service (depends on guardrails Service DNS)
./02-deploy-agent.ps1
```

Re-running `01` or `02` performs a rolling update with a fresh image tag.

## Configuration (secrets)

`01-deploy-guardrails.ps1` and `02-deploy-agent.ps1` accept a
`-GuardrailsInternalToken` parameter. If omitted a random 48-char token is
generated and stored in **both** k8s Secrets so the agent can authenticate to
guardrails. To rotate, pass the same explicit value to both scripts.

Optional Azure AI Content Safety / Language / OpenAI tokens can be passed via
`-Env @{ KEY = 'value' }` on either script and are merged into the workload's
Secret. See the script headers for the supported keys.

## Tear down

```powershell
az group delete --name rg-bankbuddy-aks --yes --no-wait
```
