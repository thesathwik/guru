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

from . import models, preprocessing, schemas
from .database import Base, SessionLocal, engine, get_db
from .storage import get_storage
from .utils import slugify

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Guru - LLM Tutor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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
            text = preprocessing.extract_text(material.filename, raw_bytes)
            text = preprocessing.clean_text(text)
            chunks = preprocessing.chunk_text(text)

            processed_path = f"{subject.slug}/processed/{material.filename}.json"
            payload = {
                "filename": material.filename,
                "subject": subject.name,
                "text": text,
                "chunks": chunks,
            }
            storage.save(processed_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

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
    get_storage().save(raw_path, data)

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


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
