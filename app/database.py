import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Configuration comes from the environment (12-factor app).
# Without DATABASE_URL we fall back to a local SQLite file, handy for local dev.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local.db")

# SQLite needs this flag because FastAPI handles requests in a thread pool.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    """Parent class of all database models."""


def get_db():
    """Dependency: one DB session per request, always closed afterwards."""
    with SessionLocal() as db:
        yield db
