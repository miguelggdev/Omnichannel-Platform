"""baseline — Sprint 3 schema completo desde modelos SQLAlchemy.

Crea las 18 tablas definidas en app/models/, con enums, índices y FKs.
Requiere extensiones: uuid-ossp, vector, pgcrypto (creadas por CI o init.sql).

Revision ID: 001_baseline
Revises:
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy  # noqa: F401 — registra el tipo VECTOR
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extensiones (idempotente) ────────────────────────────────────────
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Tablas raíz (sin FKs a otras tablas del proyecto) ────────────────
    # Nota: los enum types (plan_type, user_role, conversation_status,
    # message_direction, message_type) se crean automáticamente por
    # SQLAlchemy al ejecutar create_table con sa.Enum().
    op.create_table(
        "clients",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "plan",
            sa.Enum("free", "starter", "professional", "enterprise", name="plan_type"),
            server_default="free",
            nullable=False,
        ),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("admin_assistant_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("admin_assistant_voice_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("lead_management_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("theme_config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("suspension_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_alert_config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("alert_message", sa.String(length=1000), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # ── Tablas nivel 1 (FK solo a clients) ───────────────────────────────
    op.create_table(
        "agent_configs",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("welcome_message", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=50), server_default="gpt-4o", nullable=False),
        sa.Column("temperature", sa.Float(), server_default="0.7", nullable=False),
        sa.Column("max_tokens", sa.Integer(), server_default="1024", nullable=False),
        sa.Column("training_mode", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("similarity_threshold", sa.Float(), server_default="0.80", nullable=False),
        sa.Column("handoff_message", sa.Text(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_configs_client_id"), "agent_configs", ["client_id"], unique=False)

    op.create_table(
        "contacts",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("merged_into_id", sa.UUID(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["merged_into_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_contacts_client_id"), "contacts", ["client_id"], unique=False)

    op.create_table(
        "documents",
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("file_url", sa.String(length=1000), nullable=True),
        sa.Column("file_type", sa.String(length=100), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_client_id"), "documents", ["client_id"], unique=False)

    op.create_table(
        "quick_replies",
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_quick_replies_client_id"), "quick_replies", ["client_id"], unique=False)

    op.create_table(
        "tags",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "name", name="uq_tag_name_per_client"),
    )
    op.create_index(op.f("ix_tags_client_id"), "tags", ["client_id"], unique=False)

    op.create_table(
        "token_budgets",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("total_budget", sa.Integer(), nullable=False),
        sa.Column("used_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model_default", sa.String(length=50), server_default="gpt-4o", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_token_budgets_client_id"), "token_budgets", ["client_id"], unique=False)

    op.create_table(
        "users",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column(
            "role",
            sa.Enum("super_admin", "admin", "supervisor", "agent", name="user_role"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index(op.f("ix_users_client_id"), "users", ["client_id"], unique=False)

    op.create_table(
        "webhook_dedup",
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("external_message_id", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "external_message_id", name="uq_webhook_dedup"),
    )
    op.create_index(op.f("ix_webhook_dedup_client_id"), "webhook_dedup", ["client_id"], unique=False)

    # ── Tablas nivel 2 (FK a clients + contacts/tags/documents) ──────────
    op.create_table(
        "contact_identifiers",
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("identifier_value", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "channel", "identifier_value", name="uq_contact_identifier"),
    )
    op.create_index(op.f("ix_contact_identifiers_client_id"), "contact_identifiers", ["client_id"], unique=False)
    op.create_index(op.f("ix_contact_identifiers_contact_id"), "contact_identifiers", ["contact_id"], unique=False)

    op.create_table(
        "contact_tags",
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("tag_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_id", "tag_id", name="uq_contact_tag"),
    )
    op.create_index(op.f("ix_contact_tags_client_id"), "contact_tags", ["client_id"], unique=False)
    op.create_index(op.f("ix_contact_tags_contact_id"), "contact_tags", ["contact_id"], unique=False)
    op.create_index(op.f("ix_contact_tags_tag_id"), "contact_tags", ["tag_id"], unique=False)

    # ── Tablas nivel 2 (FK a contacts + users) ───────────────────────────
    op.create_table(
        "conversations",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "new", "bot_active", "human_active", "waiting_human",
                "waiting_client", "resolved", "archived",
                name="conversation_status",
                create_type=False,
            ),
            server_default="new",
            nullable=False,
        ),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_conversations_client_id"), "conversations", ["client_id"], unique=False)
    op.create_index(op.f("ix_conversations_contact_id"), "conversations", ["contact_id"], unique=False)

    op.create_table(
        "document_chunks",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_document_chunks_client_id"), "document_chunks", ["client_id"], unique=False)
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False)

    op.create_table(
        "internal_notes",
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_internal_notes_client_id"), "internal_notes", ["client_id"], unique=False)
    op.create_index(op.f("ix_internal_notes_contact_id"), "internal_notes", ["contact_id"], unique=False)

    # ── Tablas nivel 3 (FK a conversations) ──────────────────────────────
    op.create_table(
        "approved_responses",
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approved_responses_client_id"), "approved_responses", ["client_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", name="message_direction"),
            nullable=False,
        ),
        sa.Column(
            "message_type",
            sa.Enum(
                "text", "image", "audio", "video", "document",
                "location", "template", "interactive",
                name="message_type",
                create_type=False,
            ),
            server_default="text",
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("media_url", sa.String(length=1000), nullable=True),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("sender_type", sa.String(length=20), nullable=False),
        sa.Column("sender_id", sa.UUID(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_messages_client_id"), "messages", ["client_id"], unique=False)
    op.create_index(op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_messages_external_message_id"), "messages", ["external_message_id"], unique=False)

    op.create_table(
        "pending_responses",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("generated_response", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pending_responses_client_id"), "pending_responses", ["client_id"], unique=False)
    op.create_index(op.f("ix_pending_responses_conversation_id"), "pending_responses", ["conversation_id"], unique=False)

    op.create_table(
        "token_usage_logs",
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("model", sa.String(length=50), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=50), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_token_usage_logs_client_id"), "token_usage_logs", ["client_id"], unique=False)


def downgrade() -> None:
    # ── Nivel 3 ──────────────────────────────────────────────────────────
    op.drop_index(op.f("ix_token_usage_logs_client_id"), table_name="token_usage_logs")
    op.drop_table("token_usage_logs")
    op.drop_index(op.f("ix_pending_responses_conversation_id"), table_name="pending_responses")
    op.drop_index(op.f("ix_pending_responses_client_id"), table_name="pending_responses")
    op.drop_table("pending_responses")
    op.drop_index(op.f("ix_messages_external_message_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_client_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_approved_responses_client_id"), table_name="approved_responses")
    op.drop_table("approved_responses")

    # ── Nivel 2 ──────────────────────────────────────────────────────────
    op.drop_index(op.f("ix_internal_notes_contact_id"), table_name="internal_notes")
    op.drop_index(op.f("ix_internal_notes_client_id"), table_name="internal_notes")
    op.drop_table("internal_notes")
    op.drop_index(op.f("ix_document_chunks_document_id"), table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_client_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index(op.f("ix_conversations_contact_id"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_client_id"), table_name="conversations")
    op.drop_table("conversations")
    op.drop_index(op.f("ix_contact_tags_tag_id"), table_name="contact_tags")
    op.drop_index(op.f("ix_contact_tags_contact_id"), table_name="contact_tags")
    op.drop_index(op.f("ix_contact_tags_client_id"), table_name="contact_tags")
    op.drop_table("contact_tags")
    op.drop_index(op.f("ix_contact_identifiers_contact_id"), table_name="contact_identifiers")
    op.drop_index(op.f("ix_contact_identifiers_client_id"), table_name="contact_identifiers")
    op.drop_table("contact_identifiers")

    # ── Nivel 1 ──────────────────────────────────────────────────────────
    op.drop_index(op.f("ix_webhook_dedup_client_id"), table_name="webhook_dedup")
    op.drop_table("webhook_dedup")
    op.drop_index(op.f("ix_users_client_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_token_budgets_client_id"), table_name="token_budgets")
    op.drop_table("token_budgets")
    op.drop_index(op.f("ix_tags_client_id"), table_name="tags")
    op.drop_table("tags")
    op.drop_index(op.f("ix_quick_replies_client_id"), table_name="quick_replies")
    op.drop_table("quick_replies")
    op.drop_index(op.f("ix_documents_client_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_contacts_client_id"), table_name="contacts")
    op.drop_table("contacts")
    op.drop_index(op.f("ix_agent_configs_client_id"), table_name="agent_configs")
    op.drop_table("agent_configs")

    # ── Raíz ─────────────────────────────────────────────────────────────
    op.drop_table("clients")

    # ── Enum types ───────────────────────────────────────────────────────
    op.execute("DROP TYPE IF EXISTS message_type")
    op.execute("DROP TYPE IF EXISTS message_direction")
    op.execute("DROP TYPE IF EXISTS conversation_status")
    op.execute("DROP TYPE IF EXISTS user_role")
    op.execute("DROP TYPE IF EXISTS plan_type")
