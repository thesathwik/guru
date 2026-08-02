# Deploying to GCP (Cloud Run + Cloud SQL + GCS + Vertex AI)

Cloud Run is stateless, which changes two things from the VM setup:
uploaded files must live in GCS (they already can), and the database has
to be managed rather than a SQLite file on disk.

The live deployment of this app runs in project `bicycle-503702`
(us-central1) at <https://guru-981400413289.us-central1.run.app>. It is
deployed by `cloudbuild.yaml` (see "4b"), which is also what a push
trigger or the GitHub workflow runs; the steps below are what that was
built on, and what to repeat for a fresh project.

Set these once:

```bash
export PROJECT=your-project-id
export REGION=us-central1
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  aiplatform.googleapis.com storage.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com
```

## 1. Bucket for materials

```bash
export BUCKET=${PROJECT}-guru-materials
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access
```

## 2. Cloud SQL (PostgreSQL)

Cloud Run instances are ephemeral, so SQLite would be discarded on every
restart. This is the smallest tier; it is the main fixed cost of running
serverless here.

`--edition=enterprise` is required: new instances default to Enterprise
Plus, which rejects shared-core tiers like `db-f1-micro` outright.

```bash
gcloud sql instances create guru-pg \
  --database-version=POSTGRES_16 --edition=enterprise --tier=db-f1-micro \
  --region=$REGION --storage-size=10GB --storage-auto-increase

# Keep the password out of shell history and out of the Cloud Run
# config: generate it, store it, and never print it. Alphanumeric only,
# so it needs no URL-escaping inside the DSN.
PW=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
export INSTANCE=$(gcloud sql instances describe guru-pg \
  --format='value(connectionName)')

gcloud sql databases create guru --instance=guru-pg
gcloud sql users create guru --instance=guru-pg --password="$PW"

printf '%s' "$PW" | gcloud secrets create guru-db-password --data-file=-
printf 'postgresql://guru:%s@/guru?host=/cloudsql/%s' "$PW" "$INSTANCE" \
  | gcloud secrets create guru-database-url --data-file=-
```

### Stopping it when idle

The instance is the only always-on cost here. Stop it when you are not
using the app; the data survives, and storage still bills a little.

```bash
gcloud sql instances patch guru-pg --activation-policy=NEVER   # stop
gcloud sql instances patch guru-pg --activation-policy=ALWAYS  # start
```

Cloud Run stays up while it is stopped, but every request that touches
the database will error until you start it again.

## 3. Service account

One identity for the app, granted only what it needs: read/write the
bucket, connect to Cloud SQL, call Vertex AI. No API keys anywhere.

Account IDs must be 6-30 characters, so `guru` is rejected - hence
`guru-app`.

```bash
gcloud iam service-accounts create guru-app --display-name="Guru tutor"
export SA=guru-app@${PROJECT}.iam.gserviceaccount.com

gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
for ROLE in roles/cloudsql.client roles/aiplatform.user \
            roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA" --role=$ROLE
done
```

## 4. Deploy

Indexing itself now happens in a separate job (see 4c), so the service
only stores the upload and queues it. `--no-cpu-throttling` still matters
where no worker job is configured, because processing then falls back to
running after the response is returned - and with Cloud Run's default
throttling the CPU is cut to near zero at exactly that point.

The DSN arrives via `--set-secrets` rather than `--set-env-vars`, so the
database password is not readable in the service's configuration.

```bash
gcloud artifacts repositories create guru --repository-format=docker \
  --location=$REGION

# Build in the cloud: Cloud Run needs linux/amd64, and an Apple-silicon
# machine would otherwise produce an arm64 image that will not start.
gcloud builds submit \
  --tag $REGION-docker.pkg.dev/$PROJECT/guru/guru:initial --timeout=1800s .

gcloud run deploy guru \
  --image=$REGION-docker.pkg.dev/$PROJECT/guru/guru:initial \
  --region=$REGION \
  --service-account=$SA \
  --add-cloudsql-instances=$INSTANCE \
  --memory=2Gi --cpu=2 \
  --no-cpu-throttling \
  --timeout=600 \
  --min-instances=0 --max-instances=2 \
  --allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=$BUCKET,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION" \
  --set-secrets="DATABASE_URL=guru-database-url:latest"
```

## 4b. The deploy pipeline

`cloudbuild.yaml` is the deploy: it builds the image with the reranker
baked in, deploys the service with every flag it needs, and updates the
worker job to the same image. Run it by hand any time:

```bash
gcloud builds submit --config cloudbuild.yaml .
```

That single definition is the point. The flags have grown non-obvious -
8Gi against MAX_CONCURRENT_PROCESSING=1 because the reranker stays
resident, --no-cpu-throttling, the worker job moving in lockstep - and
getting one wrong degrades the app quietly rather than failing. Anything
that deploys should go through this file rather than restate it.

Cloud Build runs the default machine type deliberately: it is the
cheapest and the one the free tier covers. A build takes about five
minutes, against two on a larger machine, which is the right trade for
something that runs on a push.

The build runs as the Compute Engine default service account, so it needs
to be able to deploy:

```bash
export BUILD_SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$BUILD_SA" --role=roles/run.admin
gcloud iam service-accounts add-iam-policy-binding $SA \
  --member="serviceAccount:$BUILD_SA" --role=roles/iam.serviceAccountUser
```

### On every push

Connecting the repository is a one-time browser step, because Cloud Build
needs authorising against GitHub:

    console.cloud.google.com/cloud-build/triggers -> Connect Repository

The connection and the trigger must share a region, and the GitHub App
connection lives in `global`. Create the trigger there too:

```bash
gcloud builds triggers create github \
  --name=guru-deploy \
  --region=global \
  --repo-owner=your-org --repo-name=your-repo \
  --branch-pattern='^main$' \
  --build-config=cloudbuild.yaml \
  --substitutions=_TAG='$SHORT_SHA'
```

Two settings the console gets wrong if you let it: it defaults to
autodetecting the build config, which is ambiguous when the repository
root holds both a Dockerfile and a cloudbuild.yaml, and it leaves the
substitutions empty so every deploy overwrites the same image tag. Both
are worth setting explicitly.

The trigger runs as `guru-deployer`, which needs `roles/logging.logWriter`
on top of its deploy permissions - a build using a user-managed service
account cannot write its own logs without it, and fails before running a
step.

This is independent of GitHub Actions, and therefore of GitHub billing -
it is a webhook, not a runner.

### GitHub Actions

`.github/workflows/deploy.yml` does the same thing through Workload
Identity Federation, so there is no service account key in the repository
and nothing to rotate: GitHub mints an OIDC token, GCP exchanges it for
short-lived credentials, and the trust is scoped to this one repository.
It shells out to the same `cloudbuild.yaml` rather than restating the
deploy.

```bash
export PROJNUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
export REPO=your-org/your-repo

gcloud iam workload-identity-pools create github --location=global \
  --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '${REPO}'"
```

The attribute condition is the load-bearing part. Without it any
repository on GitHub could present a token and impersonate the deployer.

CI deploys under its own identity rather than the app's, so a leak of the
build pipeline does not hand over the app's data access:

```bash
gcloud iam service-accounts create guru-deployer --display-name="Guru CI deployer"
export DEPLOYER=guru-deployer@${PROJECT}.iam.gserviceaccount.com

for ROLE in roles/cloudbuild.builds.editor roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$DEPLOYER" --role=$ROLE
done
gcloud iam service-accounts add-iam-policy-binding $BUILD_SA \
  --member="serviceAccount:$DEPLOYER" --role=roles/iam.serviceAccountUser

gcloud iam service-accounts add-iam-policy-binding $DEPLOYER \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJNUM}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
```

No GitHub secrets are needed; the identifiers in the workflow are not
sensitive.

`--allow-unauthenticated` puts the app on the public internet with no
login (it has none). See "Access" below.

## 4c. The processing worker

Uploading only stores the file and queues a row; a separate Cloud Run job
does the indexing. Cloud Run reclaims idle instances and kills ones that
exceed memory, so work running inside the request's own container was
lost mid-flight, leaving materials stuck in "processing" with nothing to
resume them. Text recognition made that expensive as well as annoying.

```bash
gcloud run jobs create guru-process \
  --image=$REGION-docker.pkg.dev/$PROJECT/guru/guru:latest \
  --region=$REGION --service-account=$SA \
  --set-cloudsql-instances=$INSTANCE \
  --memory=8Gi --cpu=2 --task-timeout=3600s --max-retries=0 \
  --set-env-vars="GCS_BUCKET=$BUCKET,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,MAX_CONCURRENT_PROCESSING=2,RERANKER_ENABLED=0" \
  --set-secrets="DATABASE_URL=guru-database-url:latest" \
  --command=python --args=-m,app.process_worker

# The service starts the worker, so it needs to invoke that one job.
gcloud run jobs add-iam-policy-binding guru-process --region=$REGION \
  --member="serviceAccount:$SA" --role=roles/run.invoker

# Then point the service at it.
gcloud run services update guru --region=$REGION \
  --update-env-vars=PROCESSING_JOB=guru-process,PROCESSING_JOB_REGION=$REGION
```

`RERANKER_ENABLED=0` on the worker: it never answers a question, so there
is no reason for it to hold the cross-encoder resident. That is what buys
back `MAX_CONCURRENT_PROCESSING=2` within the same memory.

The queue is the `materials` table, not a broker, which keeps the failure
modes small. A row is claimed with `FOR UPDATE SKIP LOCKED`, so the
several executions an upload burst triggers never take the same material.
Anything still claimed after `PROCESSING_STALE_MINUTES` is assumed
abandoned and requeued, and a material that has burned
`PROCESSING_MAX_ATTEMPTS` is failed rather than retried forever. Starting
the job is fire-and-forget: if it fails the row stays queued and the next
upload's worker drains it.

Expect roughly two minutes between upload and processing starting - the
worker is a cold start pulling a multi-gigabyte image. The worker lingers
briefly after the queue empties, so a burst pays that once rather than per
file.

## 5. Move the existing materials across

Nothing needs to be recovered from the old VM. The database is
rebuildable from the files themselves, so only the bucket contents
matter.

Copy from Azure Blob Storage (run anywhere with both credentials - Cloud
Shell is easiest):

```bash
# Azure side: a read SAS URL for the container
azcopy copy \
  "https://<account>.blob.core.windows.net/materials?<SAS>" \
  "gs://$BUCKET" --recursive
```

`gcloud storage rsync` also works if you first pull the container to a
local directory.

Then rebuild the database from what is now in the bucket:

```bash
gcloud run jobs create guru-recover \
  --image=$(gcloud run services describe guru --region=$REGION --format='value(spec.template.spec.containers[0].image)') \
  --region=$REGION --service-account=$SA \
  --set-cloudsql-instances=$INSTANCE \
  --set-env-vars="GCS_BUCKET=$BUCKET,GOOGLE_CLOUD_PROJECT=$PROJECT" \
  --set-secrets="DATABASE_URL=guru-database-url:latest" \
  --command=python --args=-m,app.recover_from_storage
gcloud run jobs execute guru-recover --region=$REGION --wait
```

This recreates every subject and material from `{subject}/raw/...` and
reprocesses them (text, chunks, embeddings, figures, captions).

## Cold starts

With `--min-instances=0` the service scales to zero, and the first
request after idle pays for loading the embedding model (~220MB) - tens
of seconds. `--min-instances=1` removes that at the cost of always
running one instance.

## Reranking

Off by default, but **on** in this deployment - and effectively required
for figures to appear at all.

Without the cross-encoder, figure scores are RRF positions with no
inherent meaning, so `_relevant_images` falls back to a relative rule:
show a figure only when it beats the runner-up by `TUTOR_IMAGE_KEEP_RATIO`
(0.85). The bi-encoder is a paraphrase model, and on a subject where many
captions share a theme it cannot separate them that far - the correct
figure ranks first and is still suppressed. With the cross-encoder the
same query scores 0.78 against 0.06 for the runner-up, and a plain
threshold (`TUTOR_RERANK_MIN_SCORE`, 0.30) does the job.

It costs ~1.1GB of image size and a slower cold start, and - the part
that bites - ~1.1GB of *resident* memory on any instance that has served
a chat request, since the model is cached after first use. That instance
also serves uploads, so the reranker and material processing compete for
the same limit: enabling it on a 4Gi service with
`MAX_CONCURRENT_PROCESSING=2` OOM-kills the container on a bulk upload.
Raise memory and lower the processing concurrency together - this
deployment runs 8Gi with 1.

On Cloud Run that is affordable because memory is per-instance and billed
only while serving:

```bash
gcloud builds submit --config=cloudbuild.reranker.yaml .   # --build-arg INCLUDE_RERANKER=1
gcloud run deploy guru --image=... --memory=4Gi --update-env-vars=RERANKER_ENABLED=1
```

The GitHub Actions workflow already passes the build arg and sets the
variable, so pushes keep it enabled.

## Access

The service is public and the app has no authentication. Options, in
increasing effort: leave it (the URL is unguessable but not secret), put
Identity-Aware Proxy in front, or drop `--allow-unauthenticated` and
reach it through `gcloud run services proxy`.
