import os

# Tests always use their own throw-away SQLite file, never a real database.
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"  # off by default; one test switches it on
os.environ["SKIP_MIGRATIONS"] = "1"  # tests build their own tables; migrations have their own tests

import pytest  # noqa: E402

from app.database import Base, engine  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    """Start every test with empty tables."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
