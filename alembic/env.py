"""Alembic 마이그레이션 환경.

연결 URL 우선순위: ``-x dburl=...`` CLI 인자 → :class:`Settings.database_url`.
대상 메타데이터는 :data:`leadcrawler.schema.Base.metadata` 이므로 autogenerate 가 가능하다.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from leadcrawler.config import get_settings
from leadcrawler.schema import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


_ALLOWED_SCHEMES = ("postgresql", "sqlite")


def _db_url() -> str:
    """CLI override(-x dburl=) 가 있으면 우선, 없으면 설정값을 쓴다.

    임의 외부 DB 연결을 막기 위해 허용 scheme(postgresql/sqlite)만 통과시킨다.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("dburl") or get_settings().database_url
    if not url.startswith(_ALLOWED_SCHEMES):
        scheme = url.split(":", 1)[0]
        raise ValueError(f"허용되지 않은 DB scheme: {scheme!r} (postgresql/sqlite 만 허용)")
    return url


def run_migrations_offline() -> None:
    """오프라인(SQL 스크립트 생성) 모드."""
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """온라인(실 DB 연결) 모드."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
