"""
Alembic environment.

The URL comes from fabribuddy_core.config so migrations and the app can never disagree
about which database they are talking to.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from fabribuddy_core import config as app_config

# Importing the model modules is what registers them on Base.metadata; without
# this an autogenerate run would see an empty schema and propose dropping
# every table.
from fabribuddy_core import models  # noqa: F401,E402
from fabribuddy_core.db import Base
from fabribuddy_core.lineage import models as lineage_models  # noqa: F401,E402

config = context.config
config.set_main_option("sqlalchemy.url", app_config.DB_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=app_config.DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, so the same migration works on both backends.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
