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

## Next steps (not in this pass)

- Turn processed chunks into embeddings + a vector store for retrieval.
- Wire up an actual LLM chat interface backed by that retrieval.
- Deploy behind a proper web server / process manager on the VM.
