# infra/

Deployment and orchestration assets live here (Phases 8–9). Kept at the repo root
(outside the `src/asr` package) because these are operational configs, not importable
Python.

Planned contents:

- **Cloud Run / GKE manifests** — deploy target for the app image built from the root
  `Dockerfile` (`--target runtime`), pushed to Artifact Registry.
- **GCP Secret Manager** — prod secret definitions/wiring (local dev stays on `.env`;
  see the hook in `src/asr/config.py`).
- **Orchestration DAG** — the pipeline `ingest → features → news → research brief`,
  as Cloud Composer/Airflow, plus a lighter Cloud Scheduler → Pub/Sub → Cloud Run
  variant.

> The primary `Dockerfile` and `docker-compose.yml` stay at the repo root so the build
> context is the whole repo and `docker compose` works with zero extra flags.
