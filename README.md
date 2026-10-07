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

## Leadership review workflow

The verifier writes findings to `leader_proposals`. A finding is never published
automatically. A reviewer acts on it from www.civicgrid.org/admin/review, and the
API enforces the rules (`app/review.py`):

| Action  | Endpoint                                   | Effect |
|---------|--------------------------------------------|--------|
| Accept  | `POST /admin/proposals/{id}/approve`       | Publishes the proposed name. Title = reviewer's title, else the title on the source, else the current title, else "Mayor". |
| Correct | `POST /admin/proposals/{id}/correct`       | Publishes a reviewer-edited name/title. Reason required. Same person → updated in place, no fake transition. |
| Dismiss | `POST /admin/proposals/{id}/reject`        | Keeps the published record. `confirm_current: true` also marks it verified. |
| Retry   | `POST /admin/proposals/{id}/retry`         | Sets status `retry`. Publishes nothing and marks nothing verified. |
| Audit   | `GET /admin/review-events`                 | Append-only log: actor, reason, before/after, source, evidence. |

Guarantees:

- Each write locks the finding and the city's current leader row. If
  `expected_leader_id` doesn't match the current leader, the API returns 409.
- A finding made against an older record is *stale*. It returns 409 unless the
  reviewer sends `acknowledge_stale: true` after seeing the current record.
- `review_events` rejects UPDATE and DELETE at the database level (trigger).
- `approve` and `reject` still accept an empty body, so older clients keep working.

Public city rows now also carry `governance_type`, `leader_last_verified_at`,
`last_verified_method`, `verification_source_url`, `last_checked_at`,
`last_check_result` (`confirmed` / `under_review` / `check_failed`) and
`official_leader_page`. `GET /cities/{id}` adds `leadership_history` and
`published_changes`. Unpublished findings and reviewer identities are never public.

### Rollout

1. Run `migrations/007_review_workflow.sql` in Supabase. It is additive and idempotent.
2. Deploy this API version.
3. Deploy civicgrid-web. The web app tolerates the older API but needs this
   version for Correct, Retry, and the verification fields.
4. Verifier follow-up: write `db_leader_id`, `proposed_title` and `evidence` (the
   verbatim span) on new findings. Treat `status = 'retry'` rows as cities to
   re-check even when the page hash is unchanged.

### Tests

```bash
pytest                       # unit tests only
createdb civicgrid_test      # disposable DB; the suite DROPS its public schema
TEST_DATABASE_URL=postgresql://localhost:5432/civicgrid_test pytest
```
