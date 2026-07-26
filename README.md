# Guru - LLM Tutor

Lets a user create subjects (each becomes a folder), upload learning
materials into a subject, and automatically preprocesses each upload
(text extraction + cleaning + chunking + embedding) so it's searchable.
Each subject has its own tutor: `POST /api/subjects/{id}/chat` answers
questions grounded only in that subject's material, via Azure OpenAI.

## How it works

- **Subjects** are created via the UI and stored as a row in SQLite plus
  a folder-like prefix in storage (e.g. `social/`).
- **Materials** are uploaded into a subject and stored at
  `{subject}/raw/{filename}`. Supported types: PDF, DOCX, TXT, MD.
- After upload, a background task extracts the text, cleans it, and
  splits it into overlapping chunks, then saves the result as JSON at
  `{subject}/processed/{filename}.json`. The material's status moves
  through `uploaded -> processing -> processed` (or `error`).
- **Storage backend** is pluggable (`backend/app/storage.py`):
  - If `AZURE_STORAGE_CONNECTION_STRING` is set, files go to Azure Blob
    Storage.
  - Otherwise, files are stored on local disk under `data/materials/`.
- **PDF text extraction** uses PyMuPDF (`fitz`), which correctly
  handles complex/reordering scripts (e.g. Devanagari/Hindi) - verified
  against a real NCERT Hindi textbook PDF. An earlier version used
  `pypdf`, which reliably corrupts Devanagari text (misplaced/duplicated
  matras); don't switch back to it for PDF extraction.
- **Chunking** splits on natural boundaries (paragraphs, then lines,
  then sentences - including the Devanagari `।` terminator, then
  clauses, then words) rather than blind fixed-size character cuts.
  Each chunk records the source page it starts on.
- **Figures/diagrams** are pulled out of PDFs during processing, along
  with each figure's **caption** (the "Fig. 5.24: ..." text sitting
  beside it, falling back to the nearest body text). Captions are
  embedded, and at question time figures are ranked by how well their
  caption matches the question - a figure is shown because it depicts
  what was asked about. Ranking is *hybrid*: mostly IDF-weighted lexical
  overlap (weights taken from the subject's own chunk text, not from the
  captions - scoring against caption-derived IDF drops any query term no
  caption contains, so "what is the tyndall effect" degrades to matching
  the word "effect" and confidently returns an unrelated figure), plus
  the caption embedding. The embedding model in use is a
  paraphrase model (symmetric sentence similarity), and on short
  captions its question-to-caption scores are near noise - asking about
  the Tyndall effect ranked "Arm-wrestling" and "Meiosis" above the
  figure captioned "Demonstration of Tyndall effect". Rare query terms
  ("tyndall") are the reliable signal, so they dominate; the vector
  score is kept for wording the caption doesn't share ("how plants make
  food" -> "Photosynthesis"). An earlier version used page proximity (show
  everything sharing a page with a matching passage), which surfaced
  whatever else happened to be on that page: a blood-centrifugation
  diagram for a Tyndall-effect question. Figures with no caption text
  found near them are never surfaced. Selection is *relative* - a figure
  must beat the subject's median caption score by a margin, not hit a
  fixed threshold - because absolute cosine values differ by embedding
  model, so a hard cutoff has to be re-guessed whenever the model
  changes. Textbook PDFs are mostly non-content
  imagery (page-background textures, running headers, rule lines), so
  `preprocessing.extract_images` filters on size, aspect ratio,
  bytes-per-pixel (flat backgrounds compress to almost nothing), and
  how many pages a given image repeats across - keyed on content hash,
  since the same background is often a separate PDF object per page.
  On a real NCERT textbook this cut 126 raw images to 16 real figures.
  JPEG 2000 images (common in PDFs, unsupported by browsers) are
  converted to PNG.
- **Embeddings**: each chunk is embedded (via a single local
  multilingual model, `fastembed` - no external API, no per-call cost)
  and stored in a `chunks` table tagged with `subject_id`. Every
  subject effectively gets its own tutor/retrieval scope from one
  shared table and index - isolation comes from always filtering
  search by `subject_id`, not from separate per-subject models or
  vector stores. `GET /api/subjects/{id}/search?q=...` does a
  brute-force cosine-similarity search scoped to that subject (fine at
  this scale - a personal library of at most a few thousand chunks per
  subject).

## Running locally / on the VM

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example ../.env   # then edit ../.env with your Azure connection string
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://<host>:8000/` in a browser.

## Configuration

Copy `.env.example` to `.env` in the repo root and fill in:

- `AZURE_STORAGE_CONNECTION_STRING` - your Azure Storage account's
  connection string. Leave blank to use local disk storage instead.
- `AZURE_STORAGE_CONTAINER` - blob container name (default `materials`).
- `APP_DATA_DIR` - where SQLite (and local-mode files) are stored
  (default `./data`).
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` -
  powers the actual tutor chat via Azure AI Foundry's unified,
  OpenAI-compatible `v1` endpoint (works with any chat model in its
  catalog, not just OpenAI's - e.g. Kimi K2). Create a resource + a
  model deployment in Azure AI Foundry first. `AZURE_OPENAI_ENDPOINT`
  is the deployment's endpoint URL *without* the trailing
  `/chat/completions` (e.g.
  `https://<resource>.services.ai.azure.com/openai/v1`);
  `AZURE_OPENAI_DEPLOYMENT` is the deployment's name, not the
  underlying model's name. Without these set, the chat endpoint returns
  a 503; everything else in the app works fine without them.

## API

- `POST /api/subjects` `{ "name": "Social" }` - create a subject
- `GET /api/subjects` - list subjects with material counts
- `GET /api/subjects/{id}` - subject detail with materials + status
- `POST /api/subjects/{id}/materials` - multipart upload (`file` field)
- `GET /api/subjects/{id}/search?q=...&top_k=5` - similarity search over
  that subject's chunks (for testing retrieval quality directly)
- `POST /api/subjects/{id}/chat` `{ "message": "...", "history": [...] }`
  - retrieves relevant chunks for the subject and asks the configured
  Azure OpenAI deployment to answer grounded in them. `history` is a
  list of `{role, content}` turns from earlier in the conversation
  (kept client-side only for now, not persisted server-side). Returns
  `{ "answer": "...", "sources": [{filename, chunk_index, score}] }`.
- `GET /api/images/{id}` - serves an extracted figure's bytes
- `GET /api/subjects/{id}/figures` - diagnostic: every extracted figure
  and the caption found for it (so a missing/wrong caption is visible).
  Narrow with `contains=` (caption substring), `file=` and/or `page=` to
  check one specific figure. Add `?q=...` to see them ranked by relevance
  with no cutoff applied, and whether each would be shown - use this to
  tell a caption-extraction problem apart from a selection one, and to
  tune the figure settings against real scores.
- `DELETE /api/materials/{id}` - remove a material
- `DELETE /api/subjects/{id}` - remove a subject and its files

## Running with Docker (manual build on the VM)

`docker-compose.yml` builds the image locally from the Dockerfile - no
registry needed. This is the default (plain `docker compose` commands
use this file), and is the current way materials get deployed while
CI/CD is on hold (see below).

```bash
cd ~/guru
git pull
docker compose up -d --build   # builds + (re)starts the container
```

Or without compose:

```bash
docker build -t guru .
docker run -p 8000:8000 --env-file .env -v guru-data:/app/data guru
```

## CI/CD: build + deploy to your VM

`.github/workflows/deploy.yml` builds a Docker image on every push to
`main` (or a manual run via the Actions tab), pushes it to GitHub
Container Registry (`ghcr.io`), then SSHes into your VM to pull the new
image and restart the container with `docker compose -f docker-compose.ghcr.yml`.

**Currently on hold**: the GitHub account tied to this repo has a
billing lock that prevents any Actions job from starting at all
(unrelated to this repo - it's public, so Actions minutes are free).
Resolve it at https://github.com/settings/billing, then pushes to
`main` will build + deploy automatically again. Until then, use the
manual `docker compose up -d --build` flow above.

### One-time VM setup

1. Install Docker + the Compose plugin on the VM.
2. Create a deploy directory and your **real** `.env` there (this file
   is never committed to git):
   ```bash
   mkdir -p ~/guru
   cd ~/guru
   nano .env   # AZURE_STORAGE_CONNECTION_STRING=..., etc. (see .env.example)
   ```
3. Open the app's port (default `8000`) in the VM's firewall / Azure
   Network Security Group.
4. Generate an SSH key pair for deployments (on your own machine, not
   the VM) and add the **public** key to the VM's
   `~/.ssh/authorized_keys` for the deploy user:
   ```bash
   ssh-keygen -t ed25519 -f guru_deploy_key -N ""
   ssh-copy-id -i guru_deploy_key.pub <user>@<vm-host>
   ```

### GitHub repo secrets

Add these under **Settings > Secrets and variables > Actions**:

| Secret          | Value                                                                 |
|------------------|------------------------------------------------------------------------|
| `VM_HOST`        | VM's public IP or hostname                                            |
| `VM_USER`        | SSH username on the VM                                                |
| `VM_SSH_KEY`     | The **private** key generated above (`guru_deploy_key`, full contents) |
| `VM_SSH_PORT`    | Optional, only if SSH isn't on port 22                                |
| `GHCR_PAT`       | A GitHub Personal Access Token (classic) with `read:packages`, so the VM can `docker pull` the image. Alternatively, make the `guru` package public under the repo's **Packages** settings and drop the login step. |

`GITHUB_TOKEN` (used to *push* the image during the build job) is
provided automatically by Actions - no setup needed for that part.

### How a deploy runs

1. Push to `main` -> Actions builds the image and pushes
   `ghcr.io/<owner>/guru:latest` and `:<commit-sha>`.
2. The workflow copies `docker-compose.ghcr.yml` to `~/guru` on the VM,
   then runs `docker compose -f docker-compose.ghcr.yml pull && ... up -d`
   over SSH, so the container restarts on the new image. The named
   volume `guru-data` keeps SQLite/local files across deploys.

## Recovering the database from storage

The subjects/materials database (SQLite) is metadata only - the actual
uploaded files live in your configured storage backend (Azure Blob, or
local disk). If the database is ever lost or reset (e.g. a
misconfigured `APP_DATA_DIR`, or a volume mixup) while the underlying
files are still intact, rebuild it with:

```bash
docker exec <container-name> python -m app.recover_from_storage
```

This scans storage for `{subject}/raw/{filename}` files, recreates any
missing Subject/Material rows (subject names are best-effort
reconstructed from the folder slug), and reprocesses each recovered
file. It's safe to run more than once - anything already in the
database is left alone.

## Reprocessing existing materials

After a preprocessing change (e.g. the pypdf -> PyMuPDF switch for
better Hindi/Devanagari support), already-uploaded files won't benefit
until they're reprocessed. Rather than deleting and re-uploading them:

```bash
docker exec <container-name> python -m app.reprocess_all
```

This re-runs extraction/cleaning/chunking/embedding for every material
currently in the database and overwrites their processed output and
chunks in place.

## Note: Docker build needs internet access to Hugging Face

The embedding model (~220MB) is downloaded once during `docker build`
(not at container runtime) so the running container never needs
outbound access to Hugging Face. If a build ever fails at the
`fastembed` model-download step, that's a build-time network issue
(e.g. building somewhere without internet access), not a runtime bug.

## Next steps (not in this pass)

- Persist chat history server-side (currently client-side only, per
  subject, lost on page reload).
- HTTPS / a domain in front of the VM (e.g. Caddy or nginx as a reverse proxy).
