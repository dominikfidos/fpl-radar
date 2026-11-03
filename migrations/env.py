"""How Alembic connects to the database and finds our table definitions."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (importing registers the tables on Base.metadata)
from app.database import DATABASE_URL, Base

config = context.config
# When the app starts migrations itself (app/migrate.py) it must keep its own logging.
if config.config_file_name and not config.attributes.get("embedded"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# A URL set in code (tests) wins, otherwise the same DATABASE_URL as the application.
url = config.get_main_option("sqlalchemy.url") or DATABASE_URL
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Print the SQL instead of running it (alembic upgrade head --sql)."""
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",  # SQLite cannot ALTER much
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
