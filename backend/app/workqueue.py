"""The work queue for material processing.

Processing used to run as a FastAPI background task, inside the request's
own container. That container is not guaranteed to outlive the response:
Cloud Run reclaims idle instances and kills ones that exceed memory, and
either way the work vanished mid-flight leaving the row stuck in
"processing" with nothing to resume it. Text recognition made that worse -
minutes of work and real token spend to lose.

So the database holds the queue and a separate Cloud Run job does the
work. The queue is a table, not a message broker, which keeps the failure
modes small: a row is claimed atomically, and anything left claimed by a
worker that died is reclaimed by the next one.
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import text

from . import models
from .database import IS_SQLITE

# The Cloud Run job that drains the queue. Unset (local dev, docker
# compose) means there is nowhere to hand work to, so the caller falls
# back to processing in-process - see main.enqueue_material.
JOB_NAME = os.environ.get("PROCESSING_JOB", "")
JOB_REGION = os.environ.get("PROCESSING_JOB_REGION", os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))

# How long a claim may go unfinished before another worker assumes the
# one holding it is dead. Comfortably longer than the slowest realistic
# material (a book's worth of pages through recognition) so a slow job is
# never stolen from itself.
STALE_AFTER = timedelta(minutes=int(os.environ.get("PROCESSING_STALE_MINUTES", "45")))

# A material that dies mid-processing is retried, but not forever: if it
# is what killed the worker, retrying just kills the next one too.
MAX_ATTEMPTS = int(os.environ.get("PROCESSING_MAX_ATTEMPTS", "3"))


def enqueued(db, material) -> None:
    """Marks a material as waiting to be picked up."""
    material.status = "queued"
    material.processing_started_at = None
    db.commit()


def reclaim_stale(db) -> int:
    """Returns abandoned claims to the queue, and fails the ones that have
    used up their attempts.

    This is the part that makes the queue self-healing: without it a
    worker killed mid-material leaves that row unreachable forever, which
    is exactly the bug that moving to a job is meant to fix.
    """
    cutoff = datetime.utcnow() - STALE_AFTER
    stale = (
        db.query(models.Material)
        .filter(
            models.Material.status == "processing",
            models.Material.processing_started_at.isnot(None),
            models.Material.processing_started_at < cutoff,
        )
        .all()
    )
    for material in stale:
        if (material.attempts or 0) >= MAX_ATTEMPTS:
            material.status = "error"
            material.error_message = (
                f"Processing was interrupted {material.attempts} times without "
                "finishing. The file may be too large or malformed to index."
            )
        else:
            material.status = "queued"
        material.processing_started_at = None
    db.commit()
    return len(stale)


def claim_next(db) -> int | None:
    """Atomically takes the oldest queued material, returning its id.

    Concurrency matters because an upload burst triggers one job execution
    per file and they run at the same time. On Postgres the row is locked
    with SKIP LOCKED so two workers never take the same material; SQLite
    (local dev) is single-writer, so the plain update is already atomic
    there.
    """
    if IS_SQLITE:
        row = (
            db.query(models.Material)
            .filter(models.Material.status == "queued")
            .order_by(models.Material.uploaded_at)
            .first()
        )
        if row is None:
            return None
        material_id = row.id
    else:
        result = db.execute(
            text(
                """
                SELECT id FROM materials
                 WHERE status = 'queued'
                 ORDER BY uploaded_at
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
                """
            )
        ).first()
        if result is None:
            db.commit()
            return None
        material_id = result[0]

    material = db.get(models.Material, material_id)
    material.status = "processing"
    material.processing_started_at = datetime.utcnow()
    material.attempts = (material.attempts or 0) + 1
    db.commit()
    return material_id


def trigger() -> bool:
    """Asks Cloud Run to start a worker. Returns whether it was accepted.

    Fire-and-forget on purpose. A failure here is not worth failing the
    upload over: the row stays queued, and the next upload's worker - or
    the reclaim pass - picks it up. Extra executions are harmless too,
    since one that finds nothing to do exits immediately.
    """
    if not JOB_NAME:
        return False

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        return False

    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = google.auth.transport.requests.AuthorizedSession(credentials)
    url = (
        f"https://run.googleapis.com/v2/projects/{project}/locations/"
        f"{JOB_REGION}/jobs/{JOB_NAME}:run"
    )
    response = session.post(url, json={}, timeout=30)
    return response.status_code < 300
