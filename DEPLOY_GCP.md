# Deploying to GCP (Cloud Run + Cloud SQL + GCS + Vertex AI)

Cloud Run is stateless, which changes two things from the VM setup:
uploaded files must live in GCS (they already can), and the database has
to be managed rather than a SQLite file on disk.

Set these once:

```bash
export PROJECT=your-project-id
export REGION=us-central1
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  aiplatform.googleapis.com storage.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com
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

```bash
gcloud sql instances create guru-db \
  --database-version=POSTGRES_16 --tier=db-f1-micro --region=$REGION
gcloud sql databases create guru --instance=guru-db
gcloud sql users create guru --instance=guru-db --password='CHOOSE-A-PASSWORD'

export INSTANCE=$(gcloud sql instances describe guru-db \
  --format='value(connectionName)')
```

## 3. Service account

One identity for the app, granted only what it needs: read/write the
bucket, connect to Cloud SQL, call Vertex AI. No API keys anywhere.

```bash
gcloud iam service-accounts create guru --display-name="Guru tutor"
export SA=guru@${PROJECT}.iam.gserviceaccount.com

gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
for ROLE in roles/cloudsql.client roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA" --role=$ROLE
done
```

## 4. Deploy

`--no-cpu-throttling` matters: material processing (text extraction,
chunking, embedding) runs as a background task *after* the upload
response is returned. With Cloud Run's default throttling the CPU is cut
to near zero at that point, and uploads sit in `processing` forever.

```bash
gcloud run deploy guru \
  --source . \
  --region=$REGION \
  --service-account=$SA \
  --add-cloudsql-instances=$INSTANCE \
  --memory=2Gi --cpu=2 \
  --no-cpu-throttling \
  --timeout=600 \
  --min-instances=0 --max-instances=2 \
  --allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=$BUCKET,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,DATABASE_URL=postgresql://guru:CHOOSE-A-PASSWORD@/guru?host=/cloudsql/$INSTANCE"
```

`--allow-unauthenticated` puts the app on the public internet with no
login (it has none). See "Access" below.

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
  --set-env-vars="GCS_BUCKET=$BUCKET,GOOGLE_CLOUD_PROJECT=$PROJECT,DATABASE_URL=postgresql://guru:CHOOSE-A-PASSWORD@/guru?host=/cloudsql/$INSTANCE" \
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

Off by default. On Cloud Run it is more practical than on a small VM,
since memory is per-instance and billed only while serving: raise
`--memory=4Gi`, build with `--build-arg INCLUDE_RERANKER=1`, and set
`RERANKER_ENABLED=1`.

## Access

The service is public and the app has no authentication. Options, in
increasing effort: leave it (the URL is unguessable but not secret), put
Identity-Aware Proxy in front, or drop `--allow-unauthenticated` and
reach it through `gcloud run services proxy`.
