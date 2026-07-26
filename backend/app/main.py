import json
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import embeddings, models, preprocessing, schemas, tutor
from .database import Base, SessionLocal, apply_migrations, engine, get_db
from .storage import get_storage
from .utils import slugify

Base.metadata.create_all(bind=engine)
apply_migrations()

app = FastAPI(title="Guru - LLM Tutor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _store_images(db, storage, subject, material, raw_bytes: bytes) -> None:
    """Extracts figures/diagrams from a PDF and records which page each
    came from, so retrieval can surface them alongside text from the
    same page. Non-PDFs simply have no images."""
    db.query(models.MaterialImage).filter_by(material_id=material.id).delete()

    if not material.filename.lower().endswith(".pdf"):
        return

    images = preprocessing.extract_images(raw_bytes)
    if not images:
        return

    # Embed captions in one batch so figures can be ranked by what they
    # actually depict, not merely by which page they sit on.
    captions = [image["caption"] for image in images]
    caption_vectors = embeddings.embed_texts([c for c in captions if c])
    vector_iter = iter(caption_vectors)

    for image in images:
        path = f"{subject.slug}/images/{material.id}/{image['digest'][:16]}.{image['ext']}"
        storage.save(path, image["data"])
        caption_embedding = json.dumps(next(vector_iter)) if image["caption"] else None
        db.add(
            models.MaterialImage(
                subject_id=subject.id,
                material_id=material.id,
                page=image["page"],
                path=path,
                content_type=f"image/{'jpeg' if image['ext'] in ('jpg', 'jpeg') else image['ext']}",
                width=image["width"],
                height=image["height"],
                caption=image["caption"],
                caption_embedding=caption_embedding,
            )
        )


def process_material(material_id: int) -> None:
    """Runs in the background after upload: extracts text from the raw
    file, cleans and chunks it, and stores the processed result so an
    LLM can consume it later. Updates the material's status as it goes."""
    db: Session = SessionLocal()
    try:
        material = db.get(models.Material, material_id)
        if material is None:
            return
        subject = material.subject
        storage = get_storage()

        material.status = "processing"
        db.commit()

        try:
            raw_bytes = storage.read(material.raw_path)
            pages = preprocessing.extract_pages(material.filename, raw_bytes)
            text, chunks = preprocessing.chunk_pages(pages)
            chunk_texts = [chunk["text"] for chunk in chunks]

            processed_path = f"{subject.slug}/processed/{material.filename}.json"
            payload = {
                "filename": material.filename,
                "subject": subject.name,
                "text": text,
                "chunks": chunk_texts,
            }
            storage.save(processed_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            # Replace this material's chunks/embeddings (idempotent - safe
            # to reprocess the same material more than once).
            db.query(models.Chunk).filter_by(material_id=material.id).delete()
            vectors = embeddings.embed_texts(chunk_texts)
            for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                db.add(
                    models.Chunk(
                        subject_id=subject.id,
                        material_id=material.id,
                        chunk_index=index,
                        text=chunk["text"],
                        embedding=json.dumps(vector),
                        page=chunk["page"],
                    )
                )

            _store_images(db, storage, subject, material, raw_bytes)

            material.processed_path = processed_path
            material.chunk_count = len(chunks)
            material.char_count = len(text)
            material.status = "processed"
            material.processed_at = datetime.utcnow()
        except Exception as exc:  # noqa: BLE001 - surface any failure on the material
            material.status = "error"
            material.error_message = str(exc)

        db.commit()
    finally:
        db.close()


@app.post("/api/subjects", response_model=schemas.SubjectOut)
def create_subject(payload: schemas.SubjectCreate, db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Subject name is required")

    base_slug = slugify(name)
    slug = base_slug
    suffix = 2
    while db.query(models.Subject).filter_by(slug=slug).first() is not None:
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    subject = models.Subject(name=name, slug=slug)
    db.add(subject)
    db.commit()
    db.refresh(subject)

    # Establish the subject's "directory" up front so it's visible in
    # storage even before any material is uploaded.
    get_storage().save(f"{slug}/.keep", b"")

    out = schemas.SubjectOut.model_validate(subject)
    out.material_count = 0
    return out


@app.get("/api/subjects", response_model=list[schemas.SubjectOut])
def list_subjects(db: Session = Depends(get_db)):
    rows = (
        db.query(models.Subject, func.count(models.Material.id))
        .outerjoin(models.Material)
        .group_by(models.Subject.id)
        .order_by(models.Subject.created_at.desc())
        .all()
    )
    results = []
    for subject, count in rows:
        out = schemas.SubjectOut.model_validate(subject)
        out.material_count = count
        results.append(out)
    return results


@app.get("/api/subjects/{subject_id}", response_model=schemas.SubjectDetailOut)
def get_subject(subject_id: int, db: Session = Depends(get_db)):
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")
    out = schemas.SubjectDetailOut.model_validate(subject)
    out.material_count = len(subject.materials)
    return out


@app.delete("/api/subjects/{subject_id}")
def delete_subject(subject_id: int, db: Session = Depends(get_db)):
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")
    get_storage().delete_prefix(f"{subject.slug}/")
    db.delete(subject)
    db.commit()
    return {"ok": True}


@app.post("/api/subjects/{subject_id}/materials", response_model=schemas.MaterialOut)
async def upload_material(
    subject_id: int,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")

    ext = file.filename.lower().rsplit(".", 1)[-1] if "." in file.filename else ""
    if ext not in preprocessing.SUPPORTED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type '.{ext}'. Supported: "
            f"{', '.join(sorted(preprocessing.SUPPORTED_EXTENSIONS))}",
        )

    data = await file.read()
    raw_path = f"{subject.slug}/raw/{file.filename}"
    # get_storage().save() is a blocking call (sync Azure SDK network I/O
    # for the blob backend); run it off the event loop so one upload
    # doesn't stall every other request being served concurrently.
    await run_in_threadpool(get_storage().save, raw_path, data)

    material = models.Material(
        subject_id=subject.id,
        filename=file.filename,
        raw_path=raw_path,
        status="uploaded",
    )
    db.add(material)
    db.commit()
    db.refresh(material)

    background_tasks.add_task(process_material, material.id)

    return material


@app.get("/api/subjects/{subject_id}/materials", response_model=list[schemas.MaterialOut])
def list_materials(subject_id: int, db: Session = Depends(get_db)):
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")
    return subject.materials


@app.delete("/api/materials/{material_id}")
def delete_material(material_id: int, db: Session = Depends(get_db)):
    material = db.get(models.Material, material_id)
    if material is None:
        raise HTTPException(404, "Material not found")
    db.delete(material)
    db.commit()
    return {"ok": True}


@app.get("/api/subjects/{subject_id}/search")
def search_subject(subject_id: int, q: str, top_k: int = 5, db: Session = Depends(get_db)):
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")
    if not q.strip():
        raise HTTPException(400, "Query 'q' is required")
    return embeddings.search_chunks(db, subject_id, q, top_k=top_k)


@app.get("/api/subjects/{subject_id}/figures")
def list_figures(
    subject_id: int,
    q: str | None = None,
    contains: str | None = None,
    file: str | None = None,
    page: int | None = None,
    db: Session = Depends(get_db),
):
    """Diagnostic view of a subject's extracted figures.

    Without `q`: every figure and the caption found for it, so a missing
    or wrong caption is visible directly - narrow with `contains` (a
    caption substring), `file` and/or `page` to check one figure. With
    `q`: the same figures ranked by relevance with no cutoff applied,
    which shows whether a figure was excluded because it scored poorly
    or because the selection rule filtered it out."""
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")

    rows = db.query(models.MaterialImage).filter_by(subject_id=subject_id).all()
    total = len(rows)
    captioned = sum(1 for row in rows if row.caption)

    if not q:
        selected = rows
        if contains:
            needle = contains.lower()
            selected = [r for r in rows if r.caption and needle in r.caption.lower()]
        if file:
            selected = [r for r in selected if r.material.filename == file]
        if page is not None:
            selected = [r for r in selected if r.page == page]

        return {
            "total": total,
            "with_caption": captioned,
            "matched": len(selected),
            "figures": [
                {
                    "id": row.id,
                    "url": f"/api/images/{row.id}",
                    "filename": row.material.filename,
                    "page": row.page,
                    "caption": row.caption,
                }
                for row in sorted(selected, key=lambda r: (r.material_id, r.page))
            ],
        }

    query_vector = embeddings.embed_query(q)
    scored = tutor.score_images(db, subject_id, query_vector, q)
    shown = {image["id"] for image in tutor._relevant_images(db, subject_id, query_vector, q)}
    return {
        "total": total,
        "with_caption": captioned,
        "query": q,
        "ranked": [
            {
                "id": row.id,
                "url": f"/api/images/{row.id}",
                "filename": row.material.filename,
                "page": row.page,
                "caption": row.caption,
                "score": round(score, 4),
                "shown": row.id in shown,
            }
            for score, row in scored[:15]
        ],
    }


@app.get("/api/images/{image_id}")
def get_image(image_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import Response

    image = db.get(models.MaterialImage, image_id)
    if image is None:
        raise HTTPException(404, "Image not found")
    return Response(
        content=get_storage().read(image.path),
        media_type=image.content_type,
        headers={"Cache-Control": "public, max-age=31536000"},
    )


@app.post("/api/subjects/{subject_id}/chat", response_model=schemas.ChatResponse)
def chat_with_subject(
    subject_id: int, payload: schemas.ChatRequest, db: Session = Depends(get_db)
):
    subject = db.get(models.Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")
    if not payload.message.strip():
        raise HTTPException(400, "Message is required")

    history = [turn.model_dump() for turn in payload.history]
    try:
        return tutor.answer_question(db, subject, payload.message, history)
    except tutor.TutorNotConfigured as exc:
        raise HTTPException(503, str(exc))


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
