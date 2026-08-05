import os
from datetime import datetime

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker


def _database_url() -> str:
    """DATABASE_URL wins when set (Cloud SQL / any Postgres), otherwise a
    local SQLite file. Cloud Run instances are ephemeral, so SQLite there
    would be discarded on every restart - a managed database is not
    optional once the app is serverless."""
    url = os.environ.get("DATABASE_URL")
    if url:
        # SQLAlchemy 2 needs an explicit driver; accept the plain
        # postgres:// form these services usually hand out.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    data_dir = os.environ.get(
        "APP_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data")
    )
    os.makedirs(data_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(data_dir, 'app.db')}"


DATABASE_URL = _database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    # SQLite only: allow use across the threadpool FastAPI runs sync
    # endpoints in.
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
    # Serverless instances come and go and connections idle out behind
    # the proxy; pre-ping avoids handing out a dead one.
    pool_pre_ping=not IS_SQLITE,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added to tables that already exist in deployed databases.
# Base.metadata.create_all() creates missing *tables* but never alters an
# existing one, so without this an older database keeps running against a
# table that's missing the new column.
_ADDED_COLUMNS = {
    # Existing rows predate provenance tracking and were all extracted
    # natively, so the default backfills them correctly.
    "chunks": {
        "page": "INTEGER",
        "source": "VARCHAR DEFAULT 'native'",
        "owner_id": "INTEGER",
        "school_id": "INTEGER",
    },
    "material_images": {
        "caption": "TEXT",
        "caption_embedding": "TEXT",
        "owner_id": "INTEGER",
        "school_id": "INTEGER",
    },
    # Ownership arrived after these tables existed. NULL is the shared
    # library, so every existing row keeps being visible to everyone.
    "subjects": {
        "owner_id": "INTEGER",
        "classroom_id": "INTEGER",
        "school_id": "INTEGER",
    },
    "classrooms": {"school_id": "INTEGER", "academic_year_id": "INTEGER"},
    "tests": {"owner_id": "INTEGER", "school_id": "INTEGER"},
    "test_attempts": {"user_id": "INTEGER"},
    "users": {"is_teacher": "BOOLEAN DEFAULT FALSE", "school_id": "INTEGER"},
    "materials": {
        "page_count": "INTEGER",
        "scanned_page_count": "INTEGER",
        "ocr_page_count": "INTEGER",
        "processing_started_at": "TIMESTAMP",
        "attempts": "INTEGER DEFAULT 0",
        "owner_id": "INTEGER",
        "school_id": "INTEGER",
    },
}


def apply_migrations() -> None:
    """Adds any missing columns. Uses SQLAlchemy's inspector rather than
    sqlite_master/PRAGMA so it works on Postgres too."""
    from sqlalchemy import text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all() will build it with every column
            present = {column["name"] for column in inspector.get_columns(table)}
            for column, column_type in columns.items():
                if column not in present:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                    )


# The name given to the tenant that everything predating multi-tenancy is
# moved into. Configurable so a deployment can call it what it actually is.
DEFAULT_SCHOOL_NAME = os.environ.get("DEFAULT_SCHOOL_NAME", "Default School")


def backfill_default_school() -> None:
    """Puts every pre-tenancy row into one school.

    Multi-tenancy arrived after there was data, and school_id has to be
    set on all of it or that content becomes unreachable - visible to no
    tenant at all. Runs on startup and is idempotent: with nothing left
    unassigned it does nothing, so it costs one count query per boot.
    """
    from sqlalchemy import text

    scoped = ("users", "classrooms", "subjects", "materials", "chunks",
              "material_images", "tests")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    present = [t for t in scoped if t in tables]
    if not present:
        return

    with engine.begin() as connection:
        orphaned = any(
            connection.execute(
                text(f"SELECT 1 FROM {table} WHERE school_id IS NULL LIMIT 1")
            ).first()
            for table in present
        )
        if not orphaned:
            return

        school_id = connection.execute(
            text("SELECT id FROM schools WHERE slug = :slug"), {"slug": "default"}
        ).scalar()
        if school_id is None:
            connection.execute(
                text(
                    "INSERT INTO schools (name, slug, created_at) "
                    "VALUES (:name, 'default', :now)"
                ),
                {"name": DEFAULT_SCHOOL_NAME, "now": datetime.utcnow()},
            )
            school_id = connection.execute(
                text("SELECT id FROM schools WHERE slug = 'default'")
            ).scalar()

        for table in present:
            connection.execute(
                text(f"UPDATE {table} SET school_id = :sid WHERE school_id IS NULL"),
                {"sid": school_id},
            )


def backfill_academic_structure() -> None:
    """Gives every school a current academic year, and moves any class
    roster onto the student/enrolment model.

    Rosters used to be a list of email addresses on a class. That works
    until anything academic needs to hang off the person rather than the
    invitation - attendance, marks, a report card, a sibling sharing a
    guardian. Each old row becomes a Student (identified by email, exactly
    as before) plus an Enrolment placing them in that class.

    Idempotent: with a current year on every school and class_members
    drained, it does nothing.
    """
    from sqlalchemy import text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "schools" not in tables or "academic_years" not in tables:
        return

    now = datetime.utcnow()
    with engine.begin() as connection:
        schools = [
            row[0] for row in connection.execute(text("SELECT id FROM schools")).all()
        ]
        for school_id in schools:
            year_id = connection.execute(
                text(
                    "SELECT id FROM academic_years "
                    "WHERE school_id = :s AND is_current = TRUE"
                ),
                {"s": school_id},
            ).scalar()
            if year_id is None:
                # Indian academic years run April to March; the label is a
                # starting point a school can rename, not a claim.
                start = now.year if now.month >= 4 else now.year - 1
                connection.execute(
                    text(
                        "INSERT INTO academic_years "
                        "(school_id, name, is_current, created_at) "
                        "VALUES (:s, :n, TRUE, :now)"
                    ),
                    {"s": school_id, "n": f"{start}-{str(start + 1)[-2:]}", "now": now},
                )
                year_id = connection.execute(
                    text(
                        "SELECT id FROM academic_years "
                        "WHERE school_id = :s AND is_current = TRUE"
                    ),
                    {"s": school_id},
                ).scalar()

            connection.execute(
                text(
                    "UPDATE classrooms SET academic_year_id = :y "
                    "WHERE school_id = :s AND academic_year_id IS NULL"
                ),
                {"y": year_id, "s": school_id},
            )

        if "class_members" not in tables:
            return

        # Read the old roster directly: the model is gone, which is the
        # point - nothing should still be writing to it.
        rows = connection.execute(
            text(
                "SELECT cm.id, cm.classroom_id, cm.email, cm.user_id, "
                "       c.school_id, c.academic_year_id "
                "  FROM class_members cm "
                "  JOIN classrooms c ON c.id = cm.classroom_id"
            )
        ).all()
        for member_id, classroom_id, email, user_id, school_id, year_id in rows:
            student_id = connection.execute(
                text(
                    "SELECT id FROM students WHERE school_id = :s AND email = :e"
                ),
                {"s": school_id, "e": email},
            ).scalar()
            if student_id is None:
                connection.execute(
                    text(
                        "INSERT INTO students "
                        "(school_id, full_name, email, user_id, status, created_at) "
                        "VALUES (:s, :n, :e, :u, 'active', :now)"
                    ),
                    # No name was ever collected, so the address stands in
                    # until somebody edits it.
                    {"s": school_id, "n": email, "e": email, "u": user_id, "now": now},
                )
                student_id = connection.execute(
                    text("SELECT id FROM students WHERE school_id = :s AND email = :e"),
                    {"s": school_id, "e": email},
                ).scalar()

            exists = connection.execute(
                text(
                    "SELECT 1 FROM enrolments "
                    "WHERE student_id = :st AND classroom_id = :c"
                ),
                {"st": student_id, "c": classroom_id},
            ).first()
            if not exists:
                connection.execute(
                    text(
                        "INSERT INTO enrolments "
                        "(school_id, student_id, classroom_id, academic_year_id, enrolled_on) "
                        "VALUES (:s, :st, :c, :y, :now)"
                    ),
                    {"s": school_id, "st": student_id, "c": classroom_id,
                     "y": year_id, "now": now},
                )
            connection.execute(
                text("DELETE FROM class_members WHERE id = :id"), {"id": member_id}
            )
