"""Create the complete initial PostgreSQL schema.

Revision ID: 20260804_0001
Revises:
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260804_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


taskstatus = postgresql.ENUM(
    "uploading",
    "queued",
    "running",
    "succeeded",
    "failed",
    "deleted",
    name="taskstatus",
    create_type=False,
)
outputmode = postgresql.ENUM("plain", "bilingual", name="outputmode", create_type=False)
pdfoutputformat = postgresql.ENUM("pdf", "docx", name="pdfoutputformat", create_type=False)
translateimagesoption = postgresql.ENUM(
    "yes", "no", name="translateimagesoption", create_type=False
)
refinemode = postgresql.ENUM("none", "double_pass", name="refinemode", create_type=False)
footnotemode = postgresql.ENUM(
    "bilingual", "translation_only", "skip", name="footnotemode", create_type=False
)
feedbackstatus = postgresql.ENUM(
    "pending", "adopted", "rejected", name="feedbackstatus", create_type=False
)
feedbacktype = postgresql.ENUM(
    "terminology",
    "grammar",
    "format",
    "omission",
    "other",
    name="feedbacktype",
    create_type=False,
)
modeltype = postgresql.ENUM("translation", "vl", name="modeltype", create_type=False)
termpriority = postgresql.ENUM("strict", "preferred", name="termpriority", create_type=False)


def _timestamp_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def upgrade() -> None:
    # pg_trgm is shared cluster functionality.  It is installed here when needed,
    # but deliberately left in place by downgrade().
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        "CREATE TYPE taskstatus AS ENUM "
        "('uploading', 'queued', 'running', 'succeeded', 'failed', 'deleted')"
    )
    op.execute("CREATE TYPE outputmode AS ENUM ('plain', 'bilingual')")
    op.execute("CREATE TYPE pdfoutputformat AS ENUM ('pdf', 'docx')")
    op.execute("CREATE TYPE translateimagesoption AS ENUM ('yes', 'no')")
    op.execute("CREATE TYPE refinemode AS ENUM ('none', 'double_pass')")
    op.execute(
        "CREATE TYPE footnotemode AS ENUM ('bilingual', 'translation_only', 'skip')"
    )
    op.execute("CREATE TYPE feedbackstatus AS ENUM ('pending', 'adopted', 'rejected')")
    op.execute(
        "CREATE TYPE feedbacktype AS ENUM "
        "('terminology', 'grammar', 'format', 'omission', 'other')"
    )
    op.execute("CREATE TYPE modeltype AS ENUM ('translation', 'vl')")
    op.execute("CREATE TYPE termpriority AS ENUM ('strict', 'preferred')")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=True),
        sa.Column(
            "auth_source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'local'"),
        ),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("oa_id", sa.String(length=64), nullable=True),
        sa.Column("partner_id", sa.String(length=64), nullable=True),
        sa.Column("partner_name", sa.String(length=128), nullable=True),
        sa.Column("oa_employed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "auth_source IN ('local', 'oa')", name="ck_users_auth_source"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_oa_id", "users", ["oa_id"], unique=False)
    op.create_index(
        "uq_users_oa_id_not_null",
        "users",
        ["oa_id"],
        unique=True,
        postgresql_where=sa.text("oa_id IS NOT NULL"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "model_configs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("model_type", modeltype, nullable=False),
        sa.Column("model_id", sa.String(length=256), nullable=False),
        sa.Column("api_base_url", sa.String(length=512), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_model_active_type",
        "model_configs",
        ["model_type"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "term_entries",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("source_term", sa.String(length=512), nullable=False),
        sa.Column(
            "source_normalized",
            sa.String(length=512),
            sa.Computed("lower(trim(source_term))", persisted=True),
            nullable=False,
        ),
        sa.Column("target_term", sa.String(length=512), nullable=False),
        sa.Column("lang_pair", sa.String(length=32), nullable=False),
        sa.Column("domain", sa.String(length=128), nullable=True),
        sa.Column(
            "priority",
            termpriority,
            nullable=False,
            server_default=sa.text("'preferred'"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lang_pair", "source_normalized", name="uq_term_lang_source_norm"
        ),
    )
    op.create_index(
        "ix_term_entries_lang_pair", "term_entries", ["lang_pair"], unique=False
    )
    op.create_index(
        "ix_term_entries_source_term", "term_entries", ["source_term"], unique=False
    )
    op.create_index(
        "ix_term_lang_source",
        "term_entries",
        ["lang_pair", "source_term"],
        unique=False,
    )

    op.create_table(
        "translation_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("file_ext", sa.String(length=16), nullable=False),
        sa.Column("source_lang", sa.String(length=16), nullable=False, server_default=sa.text("'auto'")),
        sa.Column("target_lang", sa.String(length=16), nullable=False),
        sa.Column(
            "output_mode",
            outputmode,
            nullable=False,
            server_default=sa.text("'plain'"),
        ),
        sa.Column(
            "pdf_output_format",
            pdfoutputformat,
            nullable=False,
            server_default=sa.text("'pdf'"),
        ),
        sa.Column(
            "translate_images",
            translateimagesoption,
            nullable=False,
            server_default=sa.text("'no'"),
        ),
        sa.Column(
            "refine_mode",
            refinemode,
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column(
            "footnote_mode",
            footnotemode,
            nullable=False,
            server_default=sa.text("'bilingual'"),
        ),
        sa.Column("source_object", sa.String(length=512), nullable=False),
        sa.Column("result_object", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            taskstatus,
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_translation_tasks_progress",
        ),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_translation_tasks_heartbeat_at",
        "translation_tasks",
        ["heartbeat_at"],
        unique=False,
    )
    op.create_index(
        "ix_translation_tasks_owner", "translation_tasks", ["owner"], unique=False
    )
    op.create_index(
        "ix_translation_tasks_status", "translation_tasks", ["status"], unique=False
    )

    op.create_table(
        "quality_feedbacks",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("feedback_type", feedbacktype, nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column(
            "status",
            feedbackstatus,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)",
            name="ck_feedback_rating",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["translation_tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["username"], ["users.username"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quality_feedbacks_task_id",
        "quality_feedbacks",
        ["task_id"],
        unique=False,
    )

    op.create_table(
        "translation_memories",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column(
            "source_normalized",
            sa.Text(),
            sa.Computed("lower(trim(source_text))", persisted=True),
            nullable=False,
        ),
        sa.Column("target_text", sa.Text(), nullable=False),
        sa.Column("lang_pair", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("domain", sa.String(length=128), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(
            ["task_id"], ["translation_tasks.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lang_pair", "source_normalized", name="uq_tm_lang_source_norm"
        ),
    )
    op.create_index(
        "ix_translation_memories_lang_pair",
        "translation_memories",
        ["lang_pair"],
        unique=False,
    )
    op.create_index(
        "ix_translation_memories_task_id",
        "translation_memories",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_tm_lang_source",
        "translation_memories",
        ["lang_pair", "source_normalized"],
        unique=False,
    )
    op.create_index(
        "ix_tm_source_trgm",
        "translation_memories",
        ["source_normalized"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"source_normalized": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_table("translation_memories")
    op.drop_table("quality_feedbacks")
    op.drop_table("translation_tasks")
    op.drop_table("term_entries")
    op.drop_table("system_config")
    op.drop_table("model_configs")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS termpriority")
    op.execute("DROP TYPE IF EXISTS modeltype")
    op.execute("DROP TYPE IF EXISTS feedbacktype")
    op.execute("DROP TYPE IF EXISTS feedbackstatus")
    op.execute("DROP TYPE IF EXISTS footnotemode")
    op.execute("DROP TYPE IF EXISTS refinemode")
    op.execute("DROP TYPE IF EXISTS translateimagesoption")
    op.execute("DROP TYPE IF EXISTS pdfoutputformat")
    op.execute("DROP TYPE IF EXISTS outputmode")
    op.execute("DROP TYPE IF EXISTS taskstatus")
