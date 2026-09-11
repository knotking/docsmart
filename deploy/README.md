# Deploying TermGuard to GCP

TermGuard was built so that moving to GCP is configuration, not a rewrite. Two
constraints in `CLAUDE.md` are what buy that:

> **8.** All document bytes go through `storage.ObjectStore` … the local backend is a
> directory; the GCP backend is a GCS bucket. Swapping them is configuration, never a
> code change.
>
> **9.** No cloud-specific code outside `storage.py` and `config.py`. Database access goes
> through SQLModel with a URL from config, so SQLite and Cloud SQL Postgres are the same
> code path.

## What changes

| Concern | Local | GCP |
| --- | --- | --- |
| Document bytes | `data/blobs/` via `LocalObjectStore` | GCS bucket via `GCSObjectStore` |
| Database | SQLite file | Cloud SQL for PostgreSQL |
| Compute | `make api` | Cloud Run (one container, API + dashboard) |
| LLM key | `ANTHROPIC_API_KEY` in the shell | Secret Manager, mounted as the same variable |
| Access control | none (single user) | Cloud Run IAM — TermGuard still has no auth of its own |

The switch is these variables:

```bash
TERMGUARD_STORAGE=gcs
TERMGUARD_GCS_BUCKET=my-termguard-docs
TERMGUARD_DB_URL=postgresql+psycopg://user@/termguard?host=/cloudsql/PROJECT:REGION:INSTANCE
```

Nothing under `termguard/` is edited. `GCSObjectStore` is already written and ships in the
image; it is imported lazily, which is why the local demo never needs
`google-cloud-storage` installed.

## One-time setup

```bash
PROJECT=my-project
REGION=us-central1

gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  storage.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT"

# Object store. Uniform access + versioning: the blobs are content-addressed and
# immutable, so object versioning is belt-and-braces against an accidental delete.
gsutil mb -p "$PROJECT" -l "$REGION" "gs://${PROJECT}-termguard-docs"
gsutil uniformbucketlevelaccess set on "gs://${PROJECT}-termguard-docs"
gsutil versioning set on "gs://${PROJECT}-termguard-docs"

# Database
gcloud sql instances create termguard-pg --project "$PROJECT" \
  --database-version POSTGRES_15 --tier db-g1-small --region "$REGION"
gcloud sql databases create termguard --instance termguard-pg --project "$PROJECT"
gcloud sql users create termguard --instance termguard-pg --project "$PROJECT"

# Image registry
gcloud artifacts repositories create termguard --repository-format docker \
  --location "$REGION" --project "$PROJECT"

# Service account, least privilege
gcloud iam service-accounts create termguard-run --project "$PROJECT"
SA="termguard-run@${PROJECT}.iam.gserviceaccount.com"
gsutil iam ch "serviceAccount:${SA}:roles/storage.objectAdmin" \
  "gs://${PROJECT}-termguard-docs"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${SA}" --role roles/cloudsql.client

# LLM key (only needed for live judging; the pipeline runs from fixtures without it)
printf %s "$ANTHROPIC_API_KEY" | gcloud secrets create anthropic-api-key \
  --project "$PROJECT" --data-file=-
gcloud secrets add-iam-policy-binding anthropic-api-key --project "$PROJECT" \
  --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor
```

## Deploy

```bash
./deploy/deploy-gcp.sh "$PROJECT" "$REGION"
```

The `Dockerfile` sits at the repo root (where `gcloud builds submit --tag` expects it)
and builds the dashboard and the API into one image: the built assets are served by
FastAPI from the same origin, so there is no CORS setup and no second service.

The service starts with `--no-allow-unauthenticated`. TermGuard deliberately ships no
authentication (`CLAUDE.md`, "what not to build"), so IAM is what stands in front of it.

## Things worth knowing before you run it in anger

**Cloud Run is stateless.** `TERMGUARD_OUT_DIR` points at `/tmp`, which is per-instance
and vanishes on scale-down. That is fine: `data/out/` only ever holds *exports*. The
authoritative copy of every document version is in the object store, addressed by content
hash, and the API serves any version from there (`/documents/{id}/versions/{n}/download`).
Nothing is lost when an instance dies.

**Runs are long.** A 26-document corpus takes ~30 seconds; a real one will take longer.
The pipeline currently runs on a background thread inside the request container, with the
Cloud Run timeout set to 900s. Past a few hundred documents, move `run_pipeline` to Cloud
Tasks or a Cloud Run job — the function already takes a `progress` callback, so the SSE
endpoint can be repointed at a Pub/Sub subscription without touching the pipeline.

**Migrations.** `init_db()` calls `create_all`, which creates missing tables but does not
alter existing ones. Before the first schema change in an environment that holds real
data, add Alembic. The models were written to be portable (no dialect-specific column
types, JSON via SQLAlchemy's generic type), so this is a normal Alembic setup.

**Concurrency.** The deploy sets `--workers 1` with up to 4 instances. `Run` rows are
independent, and blob writes are idempotent (content-addressed, with
`if_generation_match=0` so concurrent writers cannot clobber each other). The one shared
mutable cursor is `Document.current_version_id`; if two runs process the same document at
once, the later write wins. Serialize per-document work before running multiple
concurrent pipelines over the same corpus.

**Cost.** With `--min-instances 0` the service scales to zero. The LLM step is the only
per-run variable cost, and it is bounded: the model sees one sentence per
`needs_judgment` hit, and responses are cached by `(sentence, rule id, prompt hash,
model)`, so a re-run over an unchanged corpus makes no API calls at all.
