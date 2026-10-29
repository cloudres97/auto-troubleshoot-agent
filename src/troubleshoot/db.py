from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    environment_id: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(String(80), default="")
    target_id: Mapped[str] = mapped_column(String(128))
    target_type: Mapped[str] = mapped_column(String(40))
    symptom: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    environment_id: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(String(80), default="")
    target_id: Mapped[str] = mapped_column(String(128))
    target_type: Mapped[str] = mapped_column(String(40))
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    tool: Mapped[str] = mapped_column(String(80))
    tool_version: Mapped[str] = mapped_column(String(20))
    collected_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(20))
    raw: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(Boolean)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    at: Mapped[str] = mapped_column(String(40), default=now)
    actor: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


def database(url):
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite") and parsed.database not in (None, ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **({"connect_args": {"check_same_thread": False, "timeout": 30}} if parsed.drivername.startswith("sqlite") else {}))
    if parsed.drivername.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
    with engine.begin() as connection:
        # Serialize the additive v0.1 -> v0.2 upgrade across API/Worker starts.
        if parsed.drivername.startswith("sqlite"):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif parsed.drivername.startswith("postgresql"):
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(783192044)")
        for table in ("cases", "evidence"):
            if inspect(connection).has_table(table):
                columns = {c["name"] for c in inspect(connection).get_columns(table)}
                if "target_id" not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN target_id VARCHAR(128) NOT NULL DEFAULT ''")
                    connection.exec_driver_sql(f"UPDATE {table} SET target_id = application_id")
                if "target_type" not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN target_type VARCHAR(40) NOT NULL DEFAULT 'yarn_application'")
        Base.metadata.create_all(connection)
    return sessionmaker(engine, expire_on_commit=False)
