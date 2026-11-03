"""The Alembic migrations must build the same schema as the models, and adopt old databases safely."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401
from app.database import Base
from app.migrate import ROOT, upgrade


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'migration-test.db').as_posix()}"


def columns(engine, table):
    return {c["name"]: (type(c["type"]).__name__, c["nullable"]) for c in inspect(engine).get_columns(table)}


def test_there_is_exactly_one_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0002"]


def test_migrations_build_the_same_tables_as_the_models(db_url):
    upgrade(db_url)
    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    for name, table in Base.metadata.tables.items():
        model_columns = {c.name: c.nullable for c in table.columns}
        db_columns = {c: nullable for c, (_, nullable) in columns(engine, name).items()}
        assert db_columns == model_columns, name
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
    engine.dispose()


def test_running_the_migrations_twice_changes_nothing(db_url):
    upgrade(db_url)
    upgrade(db_url)  # up to date already: must not fail or duplicate anything
    engine = create_engine(db_url)
    assert "rank_history" in inspect(engine).get_table_names()
    engine.dispose()


def test_an_old_database_is_adopted_without_losing_data(db_url):
    # A database made by the old create_all: it has the first three tables and real data,
    # but no alembic_version and no rank_history.
    engine = create_engine(db_url)
    old = [Base.metadata.tables[n] for n in ("players", "snapshots", "api_cache")]
    Base.metadata.create_all(engine, tables=old)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO players (id, name, team, position) VALUES (7, 'Saka', 'ARS', 'MID')"))

    upgrade(db_url)

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT name FROM players WHERE id = 7")) == "Saka"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
    assert "rank_history" in inspect(engine).get_table_names()
    engine.dispose()


def test_downgrade_removes_only_rank_history(db_url):
    from alembic import command

    upgrade(db_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", db_url)
    config.attributes["embedded"] = True
    command.downgrade(config, "0001")
    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert "rank_history" not in tables and {"players", "snapshots", "api_cache"} <= tables
    engine.dispose()
