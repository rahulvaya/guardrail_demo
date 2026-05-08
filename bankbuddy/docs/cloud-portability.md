# Cloud portability runbook

BankBuddy is built so that no Python code change is required to move between clouds. Every cloud-coupled concern is behind an interface in `shared/bankbuddy_shared/interfaces/`. Migration is mostly a configuration change plus, occasionally, adding a new adapter class.

## Per-concern delta table

| Concern | Interface | Default (Azure-friendly) | AWS | GCP | On-prem / OSS |
|---------|-----------|--------------------------|-----|-----|----------------|
| Identity | `IAuthProvider` | `EntraAuthProvider` | `CognitoProvider` (new class) | `IdentityPlatformProvider` (new class) | `KeycloakProvider` (new class) |
| Agent runtime | `IAgentProvider` | `LangGraphAgentProvider` | `BedrockAgentProvider` | `VertexAgentProvider` | `LangGraphAgentProvider` |
| LLM | `ILLMClient` (LiteLLM) | `LLM_PROVIDER=azure-openai` | `LLM_PROVIDER=bedrock` | `LLM_PROVIDER=vertex` | `LLM_PROVIDER=ollama` or `vllm` |
| Secrets | `ISecretProvider` | `azure-kv` | `aws-sm` | `gcp-sm` | `vault` or `env` |
| Object storage (later) | (new ABC) | Blob Storage | S3 | GCS | MinIO |
| Telemetry | `ITelemetry` | App Insights via OTel | CloudWatch via OTel | Cloud Trace via OTel | Tempo + Loki + Prometheus |
| Container runtime | n/a | ACA / AKS | ECS / EKS | Cloud Run / GKE | k3s / kind / OpenShift |
| IaC (future) | n/a | Bicep | Terraform AWS | Terraform GCP | Terraform + Helm |

## Step-by-step: Azure -> AWS

1. **Identity**
   - Add `services/api/app/auth/cognito_provider.py` implementing `IAuthProvider`.
   - Register it in `services/api/app/auth/factory.py`.
   - Set `AUTH_PROVIDER=cognito` and the `COGNITO_*` env vars in `.env`.
2. **LLM**
   - Set `LLM_PROVIDER=bedrock`, `LLM_MODEL=anthropic.claude-3-5-sonnet-...`, supply AWS creds via `ISecretProvider`.
3. **Secrets**
   - Set `SECRET_PROVIDER=aws-sm`. Add `AWSSecretsManagerProvider` if not yet implemented.
4. **Agent runtime (optional)**
   - Keep `LangGraphAgentProvider` (cloud-agnostic), or implement `BedrockAgentProvider` for managed runtime.
5. **Containers**
   - Push images to ECR; deploy via ECS Fargate or EKS using the same `Dockerfile`s.
6. **Postgres**
   - Replace the `postgres` compose service with RDS for PostgreSQL; only `POSTGRES_HOST`, `POSTGRES_PORT`, and credentials change.

No changes to:
- `shared/` (interfaces / contracts)
- `services/agent/app/graph` (LangGraph definition)
- `services/agent/app/tools` (banking tools)
- `services/ui` (presentation)

## Step-by-step: Azure -> on-prem (fully OSS)

1. `AUTH_PROVIDER=keycloak` + add `KeycloakProvider` (Authlib).
2. `LLM_PROVIDER=ollama` (or `vllm`).
3. `SECRET_PROVIDER=vault`.
4. Replace cloud Postgres with the bundled `postgres` service or an internal RDS-equivalent.
5. Deploy on k3s / OpenShift with the same images.

## What you must NOT do

- Do not import vendor SDKs (msal, boto3, google-cloud-*, azure-*) anywhere in `shared/` or in agent business logic. Keep them inside the relevant adapter under `services/*/app/<concern>/<provider>.py`.
- Do not bake env-specific values into the React bundle. Use `/config` at runtime instead.
- Do not add direct DB calls from `api` or `ui` to a schema they don't own.
