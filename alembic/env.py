"""Alembic environment configuration."""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic.ddl.postgresql import PostgresqlImpl
from sqlalchemy import String, engine_from_config, inspect, pool, text

from alembic import context
from myloware.config import settings
from myloware.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger(__name__)

target_metadata = Base.metadata


class AismrPostgresqlImpl(PostgresqlImpl):
    """Preserve existing descriptive revision IDs longer than Alembic's default."""

    __dialect__ = "postgresql"

    def version_table_impl(self, **kwargs):
        table = super().version_table_impl(**kwargs)
        table.c.version_num.type = String(128)
        return table


def get_url():
    return settings.database_url


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            if connection.dialect.name == "postgresql":
                inspector = inspect(connection)
                if inspector.has_table("alembic_version"):
                    column = next(
                        c
                        for c in inspector.get_columns("alembic_version")
                        if c["name"] == "version_num"
                    )
                    if getattr(column["type"], "length", 128) < 128:
                        connection.execute(
                            text(
                                "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"
                            )
                        )
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
