"""
SQLAlchemy engine/session. SQLite for this stage; the only thing that has to
change for Postgres is FL_DB_URL, which is why queries go through the
repository modules rather than raw SQL.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config

_is_sqlite = config.DB_URL.startswith("sqlite")

engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    future=True,
)

if _is_sqlite:
    # Default SQLite journalling blocks readers while a writer holds the file.
    # A full collector pass writes for minutes, which would freeze the UI.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        # Lineage traversal walks lineage_edge by FK; without this, a deleted
        # node would leave dangling edges that the graph walker then follows.
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _flag_auto_create() -> bool:
    import os

    raw = os.environ.get("FL_AUTO_CREATE_SCHEMA")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    # On for SQLite dev, off once someone points this at a real server.
    return _is_sqlite


def init_db(create: bool | None = None):
    """
    Ensure the schema exists.

    Alembic owns the schema in production (`alembic upgrade head`). create_all
    stays available for tests and first-run local use, but is off by default
    once migrations are present so the two never disagree about the truth.
    """
    from . import models  # noqa: F401 — register mappers before create_all
    from .lineage import models as lineage_models  # noqa: F401
    from .secrets import encrypted_store  # noqa: F401 — secret_blob

    if create is None:
        create = _flag_auto_create()
    if create:
        Base.metadata.create_all(engine)


def get_session():
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
