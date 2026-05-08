# infra/

Infrastructure assets that are not service code:

- `postgres/init.sql` - bootstraps the three schemas (`app`, `agent_memory`, `bank`) and per-service users with least-privilege ownership.
- `local/` - reserved for `docker-compose` overrides used during development (e.g., hot-reload mounts). Currently empty.
- `docker/python.base.Dockerfile` - reference base layer; each service has its own Dockerfile that mirrors the same pattern.

## Future

- `k8s/` - Helm charts and Kustomize overlays for deployment to any Kubernetes cluster (EKS / AKS / GKE / OpenShift / k3s). Will arrive after Phase 1f.
- `terraform/` - cloud-provisioning modules per target cloud.
