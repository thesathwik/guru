"""One-off utility: re-runs extraction/chunking for every existing
material. Use this after a preprocessing change (e.g. switching PDF
extraction libraries) so already-uploaded files benefit without having
to be deleted and re-uploaded.

Run inside the running container:
    docker exec <container> python -m app.reprocess_all
"""
from . import models
from .database import SessionLocal
from .main import process_material


def main() -> None:
    db = SessionLocal()
    try:
        material_ids = [m.id for m in db.query(models.Material).all()]
    finally:
        db.close()

    print(f"Reprocessing {len(material_ids)} material(s)...")
    for material_id in material_ids:
        process_material(material_id)
        print(f"  Reprocessed material id={material_id}")

    print("Done.")


if __name__ == "__main__":
    main()
