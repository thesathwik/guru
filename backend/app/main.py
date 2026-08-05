import json
import os
import threading
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import auth, embeddings, models, ocr, preprocessing, schemas, testgen, tutor, workqueue
from .database import (
    Base,
    SessionLocal,
    apply_migrations,
    backfill_default_school,
    engine,
    get_db,
)
from .storage import get_storage
from .utils import slugify

Base.metadata.create_all(bind=engine)
apply_migrations()
backfill_default_school()

app = FastAPI(title="Guru - LLM Tutor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Processing one material holds the file in memory several times over:
# the raw bytes, the extracted pages, the chunk text, the embeddings, and
# every decoded figure - and the raw bytes stay alive throughout, since
# _store_images needs them at the end. Each upload schedules its own
# background task, so without a cap the peak scales with how fast someone
# clicks Upload; a batch of ten textbooks is enough to OOM-kill the
# container, which loses the work silently (SIGKILL skips the error
# handler below, stranding rows in "processing"). Bound the parallelism
# and let the rest queue up instead.
MAX_CONCURRENT_PROCESSING = int(os.environ.get("MAX_CONCURRENT_PROCESSING", "2"))
_processing_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PROCESSING)


class ProcessingError(Exception):
    """A material that cannot be indexed for a reason worth telling the
    student about, as opposed to an unexpected crash."""


def _classroom_ids(db, user: models.User) -> set[int]:
    """Classes this user belongs to, whether teaching or enrolled."""
    if user.school_id is None:
        return set()
    taught = {
        row.id
        for row in db.query(models.Classroom.id)
        .filter_by(teacher_id=user.id, school_id=user.school_id)
        .all()
    }
    enrolled = {
        row.classroom_id
        for row in db.query(models.ClassMember.classroom_id)
        .filter_by(user_id=user.id)
        .all()
    }
    return taught | enrolled


def _can_see(row, user: models.User, classroom_ids: set[int] | None = None) -> bool:
    """Tenant first, then the three tiers within it.

    The school check is unconditional and comes before everything else: no
    combination of ownership, class membership or role should ever reach
    across tenants, so it is not something the later branches can override.

    Within a school: a row belonging to a class is visible to that class;
    otherwise owner_id NULL is the school's library and a set owner_id is
    personal. Materials and figures have no classroom of their own - they
    inherit it from their subject, which is what keeps retrieval simple.
    """
    if user.school_id is None or getattr(row, "school_id", None) != user.school_id:
        return False
    if getattr(row, "classroom_id", None) is not None:
        return row.classroom_id in (classroom_ids or set())
    return row.owner_id is None or row.owner_id == user.id


def _can_edit(row, user: models.User) -> bool:
    """Who may change a thing: the class's teacher for class material, an
    administrator for the shared library, the owner for personal."""
    classroom_id = getattr(row, "classroom_id", None)
    if classroom_id is not None:
        classroom = getattr(row, "classroom", None)
        return bool(classroom and classroom.teacher_id == user.id) or user.is_admin
    return user.is_admin if row.owner_id is None else row.owner_id == user.id


def _visible_subject(db, subject_id: int, user: models.User) -> models.Subject:
    subject = db.get(models.Subject, subject_id)
    if subject is None or not _can_see(subject, user, _classroom_ids(db, user)):
        # 404 rather than 403 for something that exists but is not theirs:
        # a different status would confirm it exists.
        raise HTTPException(404, "Subject not found")
    return subject


def _publishes_to_class(subject: models.Subject, user: models.User) -> bool:
    """Whether this user's upload into this subject is for everyone who can
    see it, or just for themselves.

    A teacher adding to their own class is publishing to the class; a
    student in that same class is adding a private note. Both land in the
    same subject, told apart by owner_id exactly as before.
    """
    if subject.classroom_id is not None:
        return bool(subject.classroom and subject.classroom.teacher_id == user.id)
    if subject.owner_id is None:
        return user.is_admin
    return False


def _visible_materials(subject: models.Subject, user: models.User) -> list:
    # Materials carry no classroom of their own - reaching here means the
    # subject was already authorised, so owner_id alone decides.
    return [m for m in subject.materials if m.owner_id is None or m.owner_id == user.id]


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
                school_id=subject.school_id,
                subject_id=subject.id,
                material_id=material.id,
                owner_id=material.owner_id,
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
    LLM can consume it later. Updates the material's status as it goes.

    Waits for a free slot rather than running immediately - see
    MAX_CONCURRENT_PROCESSING."""
    with _processing_slots:
        _process_material(material_id)


def _process_material(material_id: int) -> None:
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
            reports = preprocessing.page_reports(material.filename, raw_bytes)
            scanned = [r["page"] for r in reports if r["is_scan"]]
            material.page_count = len(reports)
            material.scanned_page_count = len(scanned)

            # Scanned pages carry no text layer, so read them off the page
            # image instead. Only those pages: the rest already have text
            # that is exact and free.
            recognised: dict[int, str] = {}
            if scanned and ocr.ENABLED:
                recognised = ocr.recognise_pages(
                    storage, subject.slug, raw_bytes, scanned
                )
                for page_number, page_text in recognised.items():
                    pages[page_number - 1] = page_text
            material.ocr_page_count = len(recognised)

            text, chunks = preprocessing.chunk_pages(pages)
            chunk_texts = [chunk["text"] for chunk in chunks]

            # A scanned document yields no text at all, and used to be
            # marked "processed" with zero chunks: the material looked fine
            # in the UI while contributing nothing to any answer. Fail it
            # loudly instead, and say why, so the upload is not silently
            # useless.
            if not chunk_texts:
                if scanned:
                    detail = (
                        "text recognition read nothing from them"
                        if ocr.ENABLED
                        else "they have no readable text layer"
                    )
                    raise ProcessingError(
                        f"This looks scanned or photographed: {len(scanned)} of "
                        f"{len(reports)} pages are images and {detail}, so nothing "
                        "could be indexed."
                    )
                raise ProcessingError("No readable text could be extracted from this file.")

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
                        school_id=subject.school_id,
                        subject_id=subject.id,
                        material_id=material.id,
                        # Carried onto the chunk so retrieval can exclude
                        # other people's material without a join.
                        owner_id=material.owner_id,
                        chunk_index=index,
                        text=chunk["text"],
                        embedding=json.dumps(vector),
                        page=chunk["page"],
                        # A chunk spanning a page break is attributed to the
                        # page it starts on, which is the same page its
                        # citation points at.
                        source="ocr" if chunk["page"] in recognised else "native",
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
def create_subject(
    payload: schemas.SubjectCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Subject name is required")

    base_slug = slugify(name)
    slug = base_slug
    suffix = 2
    while db.query(models.Subject).filter_by(slug=slug).first() is not None:
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    classroom_id = None
    if payload.classroom_id is not None:
        classroom = db.get(models.Classroom, payload.classroom_id)
        if classroom is None or classroom.teacher_id != user.id:
            raise HTTPException(403, "You do not teach that class")
        classroom_id = classroom.id

    # An administrator can add to the shared library; a teacher can add to
    # a class they run; anyone else creates a subject only they can see.
    shared = bool(payload.shared and user.is_admin) and classroom_id is None
    subject = models.Subject(
        school_id=user.school_id,
        name=name,
        slug=slug,
        classroom_id=classroom_id,
        owner_id=None if (shared or classroom_id is not None) else user.id,
    )
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
def list_subjects(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    mine = _classroom_ids(db, user)
    # The shared library and this user's own subjects, plus the subjects of
    # every class they are in. Built as a list so "no classes" simply omits
    # that clause rather than putting a Python False into the SQL.
    conditions = [
        models.Subject.classroom_id.is_(None)
        & (
            (models.Subject.owner_id.is_(None))
            | (models.Subject.owner_id == user.id)
        )
    ]
    if mine:
        conditions.append(models.Subject.classroom_id.in_(mine))

    if user.school_id is None:
        return []
    subjects = (
        db.query(models.Subject)
        .filter(models.Subject.school_id == user.school_id, or_(*conditions))
        .order_by(models.Subject.created_at.desc())
        .all()
    )

    results = []
    for subject in subjects:
        out = schemas.SubjectOut.model_validate(subject)
        # Counts what this user can actually see, not what exists: a
        # shared subject shows a different number to different people.
        out.material_count = len(_visible_materials(subject, user))
        out.shared = subject.owner_id is None and subject.classroom_id is None
        out.classroom_id = subject.classroom_id
        out.classroom_name = subject.classroom.name if subject.classroom else None
        results.append(out)
    return results


@app.get("/api/subjects/{subject_id}", response_model=schemas.SubjectDetailOut)
def get_subject(
    subject_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    subject = _visible_subject(db, subject_id, user)
    visible = _visible_materials(subject, user)
    out = schemas.SubjectDetailOut.model_validate(subject)
    out.materials = [schemas.MaterialOut.model_validate(m) for m in visible]
    out.material_count = len(visible)
    out.shared = subject.owner_id is None and subject.classroom_id is None
    out.classroom_id = subject.classroom_id
    out.classroom_name = subject.classroom.name if subject.classroom else None
    return out


@app.delete("/api/subjects/{subject_id}")
def delete_subject(
    subject_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    subject = _visible_subject(db, subject_id, user)
    if not _can_edit(subject, user):
        raise HTTPException(403, "This subject is not yours to delete")
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
    user: models.User = Depends(auth.current_user),
):
    subject = _visible_subject(db, subject_id, user)

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

    # Publishing is the teacher's act in a class and the administrator's
    # in the shared library; everyone else's upload is personal, even in a
    # subject they share - which is what lets a student keep their own
    # notes beside the class textbook.
    shared_upload = _publishes_to_class(subject, user)
    material = models.Material(
        school_id=subject.school_id,
        subject_id=subject.id,
        owner_id=None if shared_upload else user.id,
        filename=file.filename,
        raw_path=raw_path,
        status="queued",
    )
    db.add(material)
    db.commit()
    db.refresh(material)

    if workqueue.JOB_NAME:
        # Fire and forget. If the job cannot be started the row simply
        # stays queued: the next upload's worker drains it, so a transient
        # failure here costs a delay rather than the material. Falling
        # back to in-process work instead would risk two workers on the
        # same row and reintroduce exactly the fragility this replaces.
        workqueue.trigger()
    else:
        # No worker job configured - local dev and docker compose - so do
        # it here, as this always used to.
        background_tasks.add_task(process_material, material.id)

    return material


@app.get("/api/subjects/{subject_id}/materials", response_model=list[schemas.MaterialOut])
def list_materials(
    subject_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    return _visible_materials(_visible_subject(db, subject_id, user), user)


@app.delete("/api/materials/{material_id}")
def delete_material(
    material_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    material = db.get(models.Material, material_id)
    if material is None or material.school_id != user.school_id or not _can_see(material, user):
        raise HTTPException(404, "Material not found")
    if not _can_edit(material, user):
        raise HTTPException(403, "This material is not yours to delete")
    db.delete(material)
    db.commit()
    return {"ok": True}


@app.get("/api/subjects/{subject_id}/search")
def search_subject(
    subject_id: int,
    q: str,
    top_k: int = 5,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    _visible_subject(db, subject_id, user)
    if not q.strip():
        raise HTTPException(400, "Query 'q' is required")
    return embeddings.search_chunks(
        db, subject_id, q, top_k=top_k, user_id=user.id, school_id=user.school_id
    )


@app.get("/api/subjects/{subject_id}/figures")
def list_figures(
    subject_id: int,
    q: str | None = None,
    contains: str | None = None,
    file: str | None = None,
    page: int | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """Diagnostic view of a subject's extracted figures.

    Without `q`: every figure and the caption found for it, so a missing
    or wrong caption is visible directly - narrow with `contains` (a
    caption substring), `file` and/or `page` to check one figure. With
    `q`: the same figures ranked by relevance with no cutoff applied,
    which shows whether a figure was excluded because it scored poorly
    or because the selection rule filtered it out."""
    _visible_subject(db, subject_id, user)

    rows = (
        db.query(models.MaterialImage)
        .filter(
            models.MaterialImage.subject_id == subject_id,
            models.MaterialImage.school_id == user.school_id,
            (models.MaterialImage.owner_id.is_(None))
            | (models.MaterialImage.owner_id == user.id),
        )
        .all()
    )
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
    scored, reranked = tutor.score_images(
        db, subject_id, query_vector, q, user.id, user.school_id
    )
    shown = {image["id"] for image in tutor._relevant_images(db, subject_id, query_vector, q)}
    return {
        "total": total,
        "with_caption": captioned,
        "query": q,
        # Which scale the scores are on: a cross-encoder relevance
        # probability, or raw RRF positions if the reranker is off.
        "reranked": reranked,
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
def get_image(
    image_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """Figure bytes, scoped like everything else.

    A browser cannot put an Authorization header on an <img src>, so the
    frontend fetches these and renders them as blob URLs. That is the
    price of not having a figure from someone's personal notes readable
    by anyone who guesses an id.
    """
    from fastapi.responses import Response

    image = db.get(models.MaterialImage, image_id)
    if image is None or image.school_id != user.school_id or not _can_see(image, user):
        raise HTTPException(404, "Image not found")
    return Response(
        content=get_storage().read(image.path),
        media_type=image.content_type,
        # Private, not public: these responses are per-user now, and a
        # shared cache must not hand one student's figure to another.
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/api/subjects/{subject_id}/chat", response_model=schemas.ChatResponse)
def chat_with_subject(
    subject_id: int,
    payload: schemas.ChatRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    subject = _visible_subject(db, subject_id, user)
    if not payload.message.strip():
        raise HTTPException(400, "Message is required")

    history = [turn.model_dump() for turn in payload.history]
    try:
        return tutor.answer_question(db, subject, payload.message, history, user=user)
    except tutor.TutorNotConfigured as exc:
        raise HTTPException(503, str(exc))


def _own_test(db, test_id: int, user: models.User) -> models.Test:
    test = db.get(models.Test, test_id)
    if test is not None and test.school_id != user.school_id:
        raise HTTPException(404, "Test not found")
    # owner_id NULL only for tests made before sign-in existed; they stay
    # reachable rather than becoming orphans nobody can open.
    if test is None or not (test.owner_id is None or test.owner_id == user.id):
        raise HTTPException(404, "Test not found")
    return test


def _own_attempt(db, attempt_id: int, user: models.User) -> models.TestAttempt:
    attempt = db.get(models.TestAttempt, attempt_id)
    if attempt is None or not (attempt.user_id is None or attempt.user_id == user.id):
        raise HTTPException(404, "Attempt not found")
    return attempt


def _test_summary(db, test: models.Test) -> schemas.TestSummaryOut:
    out = schemas.TestSummaryOut.model_validate(test)
    out.material_filenames = [m.filename for m in test.materials]
    submitted = [a for a in test.attempts if a.submitted_at is not None]
    out.attempt_count = len(submitted)
    out.best_score = max((a.score_points or 0.0 for a in submitted), default=None)
    return out


def _graded_answers(attempt: models.TestAttempt) -> list[schemas.GradedAnswerOut]:
    by_question = {a.question_id: a for a in attempt.answers}
    results = []
    for question in attempt.test.questions:
        answer = by_question.get(question.id)
        results.append(
            schemas.GradedAnswerOut(
                question_id=question.id,
                position=question.position,
                kind=question.kind,
                prompt=question.prompt,
                options=json.loads(question.options) if question.options else None,
                points=question.points,
                selected_option=answer.selected_option if answer else None,
                response=answer.response if answer else None,
                awarded_points=answer.awarded_points if answer else None,
                is_correct=answer.is_correct if answer else None,
                feedback=answer.feedback if answer else None,
                correct_option=question.correct_option,
                expected_answer=question.expected_answer,
                explanation=question.explanation,
                source_filename=question.source_filename,
                source_page=question.source_page,
            )
        )
    return results


def _attempt_out(attempt: models.TestAttempt) -> schemas.AttemptOut:
    return schemas.AttemptOut(
        id=attempt.id,
        test_id=attempt.test_id,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        score_points=attempt.score_points,
        max_points=attempt.max_points,
        answers=_graded_answers(attempt) if attempt.submitted_at else [],
    )


@app.post("/api/subjects/{subject_id}/tests", response_model=schemas.TestDetailOut)
def create_test(
    subject_id: int,
    payload: schemas.TestCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """Generates a test synchronously.

    Unlike material processing this is not a background task: it takes
    tens of seconds, not minutes, and doing it inline means a failure
    surfaces as a plain error the student can retry rather than a row
    stuck in a "generating" state that nothing ever resumes.
    """
    subject = _visible_subject(db, subject_id, user)

    count = max(1, min(payload.question_count, testgen.MAX_QUESTIONS))

    # Only this subject's processed materials: an unprocessed one has no
    # chunks, so it would silently contribute nothing to the test.
    materials = (
        db.query(models.Material)
        .filter(
            models.Material.subject_id == subject_id,
            models.Material.id.in_(payload.material_ids or []),
            models.Material.school_id == user.school_id,
            models.Material.status == "processed",
            (models.Material.owner_id.is_(None))
            | (models.Material.owner_id == user.id),
        )
        .all()
    )
    if not materials:
        raise HTTPException(
            400, "Select at least one processed material to build the test from"
        )

    try:
        questions = testgen.generate_questions(
            db, subject, [m.id for m in materials], count
        )
    except testgen.TestGenerationError as exc:
        raise HTTPException(422, str(exc))
    except tutor.TutorNotConfigured as exc:
        raise HTTPException(503, str(exc))

    title = (payload.title or "").strip()
    if not title:
        title = ", ".join(m.filename for m in materials[:2])
        if len(materials) > 2:
            title += f" +{len(materials) - 2} more"

    test = models.Test(
        school_id=subject.school_id,
        subject_id=subject.id,
        owner_id=user.id,
        title=title,
        question_count=len(questions),
        time_limit_minutes=payload.time_limit_minutes or None,
        max_points=sum(q.points for q in questions),
    )
    test.materials = materials
    test.questions = questions
    db.add(test)
    db.commit()
    db.refresh(test)

    return _test_detail(db, test)


def _test_detail(db, test: models.Test) -> schemas.TestDetailOut:
    out = schemas.TestDetailOut.model_validate(_test_summary(db, test).model_dump())
    out.questions = [
        schemas.TestQuestionOut(
            id=q.id,
            position=q.position,
            kind=q.kind,
            prompt=q.prompt,
            options=json.loads(q.options) if q.options else None,
            points=q.points,
        )
        for q in test.questions
    ]
    return out


@app.get("/api/subjects/{subject_id}/tests", response_model=list[schemas.TestSummaryOut])
def list_tests(
    subject_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    _visible_subject(db, subject_id, user)
    tests = (
        db.query(models.Test)
        .filter(
            models.Test.subject_id == subject_id,
            # A test is personal: it is generated from what one student
            # chose and scored against their own attempts.
            (models.Test.owner_id.is_(None)) | (models.Test.owner_id == user.id),
        )
        .order_by(models.Test.created_at.desc())
        .all()
    )
    return [_test_summary(db, t) for t in tests]


@app.get("/api/tests/{test_id}", response_model=schemas.TestDetailOut)
def get_test(
    test_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    return _test_detail(db, _own_test(db, test_id, user))


@app.delete("/api/tests/{test_id}")
def delete_test(
    test_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    test = _own_test(db, test_id, user)
    db.delete(test)
    db.commit()
    return {"ok": True}


@app.post("/api/tests/{test_id}/attempts", response_model=schemas.AttemptOut)
def start_attempt(
    test_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    test = _own_test(db, test_id, user)
    attempt = models.TestAttempt(test_id=test.id, user_id=user.id)
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return _attempt_out(attempt)


@app.post("/api/attempts/{attempt_id}/submit", response_model=schemas.AttemptOut)
def submit_attempt(
    attempt_id: int,
    payload: schemas.AttemptSubmit,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    attempt = _own_attempt(db, attempt_id, user)
    if attempt.submitted_at is not None:
        raise HTTPException(400, "This attempt has already been submitted")

    valid_ids = {q.id for q in attempt.test.questions}
    submitted = {
        a.question_id: a for a in payload.answers if a.question_id in valid_ids
    }

    # Record a row for every question, not only the answered ones, so an
    # unanswered question is marked as blank rather than left absent.
    for question in attempt.test.questions:
        incoming = submitted.get(question.id)
        db.add(
            models.TestAnswer(
                attempt_id=attempt.id,
                question_id=question.id,
                selected_option=incoming.selected_option if incoming else None,
                response=(incoming.response or "").strip() or None if incoming else None,
            )
        )
    db.flush()
    db.refresh(attempt)

    try:
        testgen.grade_attempt(db, attempt)
    except testgen.TestGenerationError as exc:
        db.rollback()
        raise HTTPException(422, f"Marking failed: {exc}")
    except tutor.TutorNotConfigured as exc:
        db.rollback()
        raise HTTPException(503, str(exc))

    db.commit()
    db.refresh(attempt)
    return _attempt_out(attempt)


@app.get("/api/attempts/{attempt_id}", response_model=schemas.AttemptOut)
def get_attempt(
    attempt_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    return _attempt_out(_own_attempt(db, attempt_id, user))


def _classroom_out(db, classroom: models.Classroom, user: models.User):
    out = schemas.ClassroomOut.model_validate(classroom)
    out.teaching = classroom.teacher_id == user.id
    out.teacher_name = classroom.teacher.display_name or classroom.teacher.email
    out.member_count = len(classroom.members)
    out.subject_count = len(classroom.subjects)
    return out


def _taught_classroom(db, classroom_id: int, user: models.User) -> models.Classroom:
    classroom = db.get(models.Classroom, classroom_id)
    if classroom is not None and classroom.school_id != user.school_id:
        raise HTTPException(404, "Class not found")
    if classroom is None or (classroom.teacher_id != user.id and not user.is_admin):
        raise HTTPException(404, "Class not found")
    return classroom


@app.get("/api/school", response_model=schemas.SchoolOut | None)
def get_school(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """The caller's school, or null if they are not in one yet."""
    if user.school_id is None:
        return None
    school = db.get(models.School, user.school_id)
    out = schemas.SchoolOut.model_validate(school)
    out.member_count = (
        db.query(models.User).filter_by(school_id=school.id).count()
    )
    return out


@app.post("/api/school", response_model=schemas.SchoolOut)
def create_school(
    payload: schemas.SchoolCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """Creates a tenant and makes the caller its first administrator.

    Only for an account that is not in one: an account belongs to exactly
    one school, and moving it would strand everything it has made.
    """
    if user.school_id is not None:
        raise HTTPException(409, "You are already in a school")

    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "School name is required")

    base = slugify(name)
    slug, suffix = base, 2
    while db.query(models.School).filter_by(slug=slug).first() is not None:
        slug, suffix = f"{base}-{suffix}", suffix + 1

    school = models.School(name=name, slug=slug)
    db.add(school)
    db.commit()
    db.refresh(school)

    user.school_id = school.id
    user.is_admin = True
    user.is_teacher = True
    db.commit()

    out = schemas.SchoolOut.model_validate(school)
    out.member_count = 1
    return out


@app.post("/api/school/invites", response_model=schemas.SchoolInviteOut)
def invite_to_school(
    payload: schemas.SchoolInviteCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require_admin),
):
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email address is required")

    existing = db.query(models.User).filter_by(email=email).first()
    if existing is not None and existing.school_id is not None:
        raise HTTPException(400, "That account already belongs to a school")

    pending = (
        db.query(models.SchoolInvite)
        .filter_by(school_id=user.school_id, email=email, claimed_at=None)
        .first()
    )
    if pending is not None:
        raise HTTPException(400, "That address has already been invited")

    invite = models.SchoolInvite(
        school_id=user.school_id,
        email=email,
        is_teacher=payload.is_teacher,
        is_admin=payload.is_admin,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


@app.get("/api/school/invites", response_model=list[schemas.SchoolInviteOut])
def list_school_invites(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require_admin),
):
    return (
        db.query(models.SchoolInvite)
        .filter_by(school_id=user.school_id, claimed_at=None)
        .order_by(models.SchoolInvite.created_at.desc())
        .all()
    )


@app.get("/api/classes", response_model=list[schemas.ClassroomOut])
def list_classes(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """Classes this user teaches or is enrolled in."""
    ids = _classroom_ids(db, user)
    if not ids:
        return []
    rooms = (
        db.query(models.Classroom)
        .filter(models.Classroom.id.in_(ids))
        .order_by(models.Classroom.created_at.desc())
        .all()
    )
    return [_classroom_out(db, room, user) for room in rooms]


@app.post("/api/classes", response_model=schemas.ClassroomOut)
def create_class(
    payload: schemas.ClassroomCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require_teacher),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Class name is required")
    classroom = models.Classroom(
        school_id=user.school_id, name=name, teacher_id=user.id
    )
    db.add(classroom)
    db.commit()
    db.refresh(classroom)
    return _classroom_out(db, classroom, user)


@app.delete("/api/classes/{classroom_id}")
def delete_class(
    classroom_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    classroom = _taught_classroom(db, classroom_id, user)
    if classroom.subjects:
        raise HTTPException(
            400,
            "Delete this class's subjects first - removing it would hide "
            f"{len(classroom.subjects)} subject(s) from everyone in it.",
        )
    db.delete(classroom)
    db.commit()
    return {"ok": True}


@app.get("/api/classes/{classroom_id}/members", response_model=list[schemas.ClassMemberOut])
def list_members(
    classroom_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """The roster. Only the teacher sees it: to a student, who else is in
    the class is not theirs to enumerate."""
    classroom = _taught_classroom(db, classroom_id, user)
    return [
        schemas.ClassMemberOut(
            id=m.id,
            email=m.email,
            display_name=m.user.display_name if m.user else None,
            joined=m.user_id is not None,
            added_at=m.added_at,
        )
        for m in sorted(classroom.members, key=lambda m: m.email)
    ]


@app.post("/api/classes/{classroom_id}/members", response_model=schemas.ClassMemberOut)
def add_member(
    classroom_id: int,
    payload: schemas.ClassMemberCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    classroom = _taught_classroom(db, classroom_id, user)
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email address is required")

    existing = (
        db.query(models.ClassMember)
        .filter_by(classroom_id=classroom.id, email=email)
        .first()
    )
    if existing is not None:
        raise HTTPException(400, "That address is already on the roster")

    # Link straight away if they have already signed up; otherwise the
    # invitation is claimed the first time they do.
    account = db.query(models.User).filter_by(email=email).first()
    member = models.ClassMember(
        classroom_id=classroom.id,
        email=email,
        user_id=account.id if account else None,
        joined_at=datetime.utcnow() if account else None,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return schemas.ClassMemberOut(
        id=member.id,
        email=member.email,
        display_name=account.display_name if account else None,
        joined=member.user_id is not None,
        added_at=member.added_at,
    )


@app.delete("/api/classes/{classroom_id}/members/{member_id}")
def remove_member(
    classroom_id: int,
    member_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    classroom = _taught_classroom(db, classroom_id, user)
    member = db.get(models.ClassMember, member_id)
    if member is None or member.classroom_id != classroom.id:
        raise HTTPException(404, "Not on this roster")
    db.delete(member)
    db.commit()
    return {"ok": True}


@app.get("/api/classes/{classroom_id}/progress")
def class_progress(
    classroom_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """How the class is doing on tests drawn from its own subjects.

    Scores only. A teacher does not get their students' personal uploads,
    their chat, or their learner profile - being able to see progress is
    not a reason to see everything.
    """
    classroom = _taught_classroom(db, classroom_id, user)
    subject_ids = [s.id for s in classroom.subjects]
    if not subject_ids:
        return {"class": classroom.name, "students": []}

    rows = (
        db.query(models.TestAttempt, models.Test, models.User)
        .join(models.Test, models.TestAttempt.test_id == models.Test.id)
        .join(models.User, models.TestAttempt.user_id == models.User.id)
        .filter(
            models.Test.subject_id.in_(subject_ids),
            models.TestAttempt.submitted_at.isnot(None),
        )
        .all()
    )

    member_ids = {m.user_id for m in classroom.members if m.user_id}
    by_student: dict[int, dict] = {}
    for attempt, test, student in rows:
        if student.id not in member_ids:
            continue  # left the class since sitting the test
        entry = by_student.setdefault(
            student.id,
            {"name": student.display_name or student.email, "attempts": []},
        )
        entry["attempts"].append(
            {
                "test": test.title,
                "score": attempt.score_points,
                "out_of": attempt.max_points,
                "percent": round(100 * (attempt.score_points or 0) / attempt.max_points)
                if attempt.max_points
                else None,
                "submitted_at": attempt.submitted_at,
            }
        )

    students = []
    for member in classroom.members:
        entry = by_student.get(member.user_id) if member.user_id else None
        attempts = entry["attempts"] if entry else []
        scored = [a["percent"] for a in attempts if a["percent"] is not None]
        students.append(
            {
                "email": member.email,
                "name": (entry or {}).get("name"),
                "joined": member.user_id is not None,
                "attempts": sorted(attempts, key=lambda a: a["submitted_at"] or 0),
                "average_percent": round(sum(scored) / len(scored)) if scored else None,
            }
        )
    return {"class": classroom.name, "students": students}


@app.get("/api/admin/users", response_model=list[schemas.AdminUserOut])
def list_users(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require_admin),
):
    return (
        db.query(models.User)
        .filter(models.User.school_id == user.school_id)
        .order_by(models.User.created_at.desc())
        .all()
    )


@app.put("/api/admin/users/{user_id}", response_model=schemas.AdminUserOut)
def set_user_role(
    user_id: int,
    payload: schemas.AdminUserUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(auth.require_admin),
):
    """Promotes an account to teacher, or demotes it.

    Admin rights are not settable here - they follow ADMIN_EMAILS, so that
    the set of people who can change the shared library lives in
    configuration rather than being editable from inside the app.
    """
    target = db.get(models.User, user_id)
    if target is not None and target.school_id != admin.school_id:
        raise HTTPException(404, "User not found")
    if target is None:
        raise HTTPException(404, "User not found")
    target.is_teacher = payload.is_teacher
    db.commit()
    db.refresh(target)
    return target


@app.get("/api/config")
def client_config():
    """What the browser needs to start a sign-in.

    The Identity Platform API key is a public project identifier, not a
    secret - access is governed by the authorised-domain list and by this
    server verifying every token. Serving it here rather than baking it
    into the JavaScript keeps the frontend the same across projects.
    """
    return {
        "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        "api_key": os.environ.get("IDENTITY_API_KEY", ""),
        "auth_domain": os.environ.get(
            "IDENTITY_AUTH_DOMAIN",
            f"{os.environ.get('GOOGLE_CLOUD_PROJECT', '')}.firebaseapp.com",
        ),
        "auth_disabled": auth.AUTH_DISABLED,
    }


@app.get("/api/me", response_model=schemas.MeOut)
def get_me(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    out = schemas.MeOut.model_validate(user)
    if user.school_id is not None:
        school = db.get(models.School, user.school_id)
        out.school_name = school.name if school else None
    return out


@app.put("/api/me/profile", response_model=schemas.LearnerProfileOut)
def update_profile(
    payload: schemas.LearnerProfileIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.current_user),
):
    """Replaces the learner profile.

    Everything here is the student's own statement about themselves, so
    it is theirs to change or clear at any time - an empty field means
    "do not tell the tutor this", and is stored as such rather than
    keeping the previous value.
    """
    profile = user.profile
    if profile is None:
        profile = models.LearnerProfile(user_id=user.id)
        db.add(profile)

    for field, value in payload.model_dump().items():
        cleaned = (value or "").strip() or None
        setattr(profile, field, cleaned)
    profile.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(profile)
    return profile


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Makes the browser check the frontend files are current.

    They are served without version hashes, so a browser holding an old
    index.html against a freshly deployed app.js gets a page that half
    works - which is exactly what happened when sign-in was added. These
    files are small; revalidating costs a 304.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
