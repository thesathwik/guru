"""Drains the material processing queue. Runs as a Cloud Run job:

    gcloud run jobs execute guru-process --region=$REGION

Claims one material at a time and processes it until nothing is left, so
a burst of uploads is absorbed by a single execution rather than needing
one per file. Several executions can run at once safely - each claims
different rows - which is what makes it fine for the service to fire one
per upload without coordinating.
"""
import os
import time

from .database import SessionLocal
from .workqueue import claim_next, reclaim_stale

# Keep looking for a moment after the queue empties. Uploads arrive in
# bursts, and the row for the second file is often committed while the
# first is still being processed; without this each straggler would need
# its own cold start.
LINGER_SECONDS = int(os.environ.get("PROCESSING_LINGER_SECONDS", "20"))


def main() -> None:
    # Imported here rather than at module scope: app.main pulls in FastAPI
    # and runs create_all/migrations on import, which the worker wants to
    # happen once, at start, not as a side effect of the module graph.
    from .main import process_material

    db = SessionLocal()
    try:
        recovered = reclaim_stale(db)
        if recovered:
            print(f"Requeued {recovered} material(s) abandoned by an earlier run.")
    finally:
        db.close()

    processed = 0
    idle_since = None

    while True:
        db = SessionLocal()
        try:
            material_id = claim_next(db)
        finally:
            db.close()

        if material_id is None:
            if idle_since is None:
                idle_since = time.monotonic()
            if time.monotonic() - idle_since >= LINGER_SECONDS:
                break
            time.sleep(2)
            continue

        idle_since = None
        print(f"Processing material {material_id}...")
        # process_material owns its own error handling and always leaves the
        # row in a terminal state; a crash escaping it is a bug, and
        # letting it kill the worker is correct - the claim goes stale and
        # is reclaimed rather than being quietly swallowed.
        process_material(material_id)
        processed += 1
        print(f"  material {material_id} done")

    print(f"Nothing left to process. Handled {processed} material(s).")


if __name__ == "__main__":
    main()
