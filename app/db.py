from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register models)

    Base.metadata.create_all(engine)
    _upgrade_existing_tables()


def _upgrade_existing_tables() -> None:
    """create_all() makes missing tables but never alters existing ones. Small additive upgrades
    for databases created by earlier versions live here (safe to run on every start)."""
    cols = {c["name"] for c in inspect(engine).get_columns("phone_numbers")}
    with engine.begin() as conn:
        if "idempotency_key" not in cols:
            conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN idempotency_key VARCHAR(128)"))
        if "paid_until" not in cols:
            conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN paid_until TIMESTAMP WITH TIME ZONE"
                              if engine.dialect.name == "postgresql" else
                              "ALTER TABLE phone_numbers ADD COLUMN paid_until TIMESTAMP"))
        if "billing_status" not in cols:
            conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN billing_status VARCHAR(16) NOT NULL DEFAULT 'ok'"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_number_idempotency "
                          "ON phone_numbers (client_id, idempotency_key)"))
