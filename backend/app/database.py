import os

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
    "chunks": {"page": "INTEGER", "source": "VARCHAR DEFAULT 'native'"},
    "material_images": {"caption": "TEXT", "caption_embedding": "TEXT"},
    "materials": {"page_count": "INTEGER", "scanned_page_count": "INTEGER"},
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
