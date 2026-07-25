"""One-off recovery tool: rebuilds the subjects/materials database from
whatever raw files are actually sitting in storage. Use this if the
database was lost (e.g. a misconfigured APP_DATA_DIR pointing outside
the persistent volume) but the underlying files survived - for example
in Azure Blob Storage, which is external to the container.

Run inside the running container:
    docker exec <container> python -m app.recover_from_storage
"""
from datetime import datetime

from . import models
from .database import Base, SessionLocal, engine
from .main import process_material
from .storage import get_storage

Base.metadata.create_all(bind=engine)


def humanize(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.split("-"))


def main() -> None:
    storage = get_storage()
    db = SessionLocal()
    recovered_material_ids = []
    try:
        raw_by_subject: dict[str, dict[str, str]] = {}
        for path in storage.list_all():
            parts = path.split("/")
            if len(parts) == 3 and parts[1] == "raw":
                raw_by_subject.setdefault(parts[0], {})[parts[2]] = path

        for slug, files in raw_by_subject.items():
            subject = db.query(models.Subject).filter_by(slug=slug).first()
            if subject is None:
                subject = models.Subject(name=humanize(slug), slug=slug)
                db.add(subject)
                db.commit()
                db.refresh(subject)
                print(f"Recovered subject: {subject.name} ({slug})")

            for filename, raw_path in files.items():
                existing = (
                    db.query(models.Material)
                    .filter_by(subject_id=subject.id, filename=filename)
                    .first()
                )
                if existing is not None:
                    continue

                material = models.Material(
                    subject_id=subject.id,
                    filename=filename,
                    raw_path=raw_path,
                    status="uploaded",
                    uploaded_at=datetime.utcnow(),
                )
                db.add(material)
                db.commit()
                db.refresh(material)
                recovered_material_ids.append(material.id)
                print(f"  Recovered material: {filename}")
    finally:
        db.close()

    for material_id in recovered_material_ids:
        process_material(material_id)
        print(f"  Reprocessed material id={material_id}")

    print(f"Recovery complete. {len(recovered_material_ids)} material(s) recovered.")


if __name__ == "__main__":
    main()
