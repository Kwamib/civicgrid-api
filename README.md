# CivicGrid API

Read-only REST API serving US municipal leadership data — 3,000+ mayors and city managers across all 50 states, DC, and US territories.

## Stack

- FastAPI (Python 3.13)
- PostgreSQL (Supabase)
- Multi-stage Docker build (~66 MB final image, runs as non-root)
- Kubernetes deploy (Helm + ArgoCD), Gateway API ingress
- GitHub Actions CI, image published to GHCR

## Endpoints

- `GET /` — service info
- `GET /health` — liveness + DB readiness
- `GET /cities` — list with filters (state, city_type, population, search)
- `GET /cities/{id}` — single city with current leader and provenance
- `GET /leaders/current` — current leaders, filterable by party and state
- `GET /stats` — aggregate counts

Swagger UI at `/docs`.

## Run locally

```bash
pip3 install -r requirements.txt
export DATABASE_URL="postgresql://..."
python3 -m uvicorn app.main:app --reload --port 8000
```

Or with Docker:

```bash
docker build -t civicgrid-api:dev .
docker run --rm -p 8000:8000 -e DATABASE_URL="..." civicgrid-api:dev
```

## Pull the published image

```bash
docker pull ghcr.io/kwamib/civicgrid-api:latest
```

## License

MIT

## Deploy to Kubernetes

A Helm chart is included at `charts/civicgrid-api/`.

```bash
# Render the chart locally to see what gets deployed
helm template civicgrid charts/civicgrid-api \
  --set database.url="$DATABASE_URL"

# Install into a cluster
helm install civicgrid charts/civicgrid-api \
  --set database.url="$DATABASE_URL"

# Upgrade after changes
helm upgrade civicgrid charts/civicgrid-api \
  --set database.url="$DATABASE_URL"
```

The chart includes:
- Deployment with non-root security context, read-only root filesystem, dropped capabilities
- Service (ClusterIP)
- ServiceAccount
- HorizontalPodAutoscaler (CPU-based, 1-3 replicas)
- Optional Secret for DATABASE_URL (use existing Secret in production)
- Liveness and readiness probes hitting `/health`
- Pod anti-affinity to spread replicas across nodes

In production, prefer `existingSecret` over passing `database.url` directly. Pair with Sealed Secrets or External Secrets Operator.
