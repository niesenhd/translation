from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _offline_sql(direction: str) -> str:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "head", sql=True)
    else:
        command.downgrade(config, "20260804_0001:base", sql=True)
    return output.getvalue().lower()


def test_initial_migration_builds_complete_postgresql_schema() -> None:
    sql = _offline_sql("upgrade")

    assert "create extension if not exists pg_trgm" in sql
    for enum_name in (
        "taskstatus",
        "outputmode",
        "pdfoutputformat",
        "translateimagesoption",
        "refinemode",
        "footnotemode",
        "feedbackstatus",
        "feedbacktype",
        "modeltype",
        "termpriority",
    ):
        assert f"create type {enum_name} as enum" in sql

    for table in (
        "users",
        "model_configs",
        "system_config",
        "term_entries",
        "translation_tasks",
        "quality_feedbacks",
        "translation_memories",
    ):
        assert f"create table {table}" in sql

    assert "id uuid not null" in sql
    assert "timestamp with time zone default now() not null" in sql
    assert "generated always as (lower(trim(source_term))) stored not null" in sql
    assert "generated always as (lower(trim(source_text))) stored not null" in sql
    assert "constraint ck_users_auth_source check" in sql
    assert "constraint ck_translation_tasks_progress check" in sql
    assert "constraint ck_feedback_rating check" in sql
    assert "references users (username)" in sql
    assert "references translation_tasks (id) on delete cascade" in sql
    assert "references translation_tasks (id) on delete set null" in sql
    assert "create unique index uq_model_active_type" in sql
    assert "where is_active" in sql
    assert "using gin (source_normalized gin_trgm_ops)" in sql

    # Operational defaults are deliberately enforced by PostgreSQL as well as
    # by the ORM so direct imports and maintenance SQL cannot create divergent
    # rows.
    assert "is_admin boolean default false not null" in sql
    assert "progress integer default 0 not null" in sql
    assert "source_lang varchar(16) default 'auto' not null" in sql


def test_downgrade_removes_owned_schema_but_keeps_shared_extension() -> None:
    sql = _offline_sql("downgrade")

    for table in (
        "translation_memories",
        "quality_feedbacks",
        "translation_tasks",
        "term_entries",
        "system_config",
        "model_configs",
        "users",
    ):
        assert f"drop table {table}" in sql
    assert sql.count("drop type if exists") == 10
    assert "drop extension" not in sql
