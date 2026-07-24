# Guru - LLM Tutor (Step 1: Subject & Material Organization)

Lets a user create subjects (each becomes a folder), upload learning
materials into a subject, and automatically preprocesses each upload
(text extraction + cleaning + chunking) so the text is ready to be fed
to an LLM in a later step.

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

## API

- `POST /api/subjects` `{ "name": "Social" }` - create a subject
- `GET /api/subjects` - list subjects with material counts
- `GET /api/subjects/{id}` - subject detail with materials + status
- `POST /api/subjects/{id}/materials` - multipart upload (`file` field)
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

## Next steps (not in this pass)

- Turn processed chunks into embeddings + a vector store for retrieval.
- Wire up an actual LLM chat interface backed by that retrieval.
- HTTPS / a domain in front of the VM (e.g. Caddy or nginx as a reverse proxy).
