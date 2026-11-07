"""users, sessions, api tokens, and per-user ownership

Revision ID: 002
Revises: 001
Create Date: 2026-11-07
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id UUID PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_sessions_user_id ON sessions (user_id);

        CREATE TABLE api_tokens (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            prefix VARCHAR(8) NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        );
        CREATE INDEX ix_api_tokens_user_id ON api_tokens (user_id);

        ALTER TABLE documents ADD COLUMN owner_id UUID REFERENCES users(id);
        CREATE INDEX ix_documents_owner_created ON documents (owner_id, created_at DESC);
        DROP INDEX ix_documents_sha256;
        CREATE UNIQUE INDEX uq_documents_owner_sha256 ON documents (owner_id, sha256);

        ALTER TABLE translation_jobs ADD COLUMN owner_id UUID REFERENCES users(id);
        CREATE INDEX ix_jobs_owner_created ON translation_jobs (owner_id, created_at DESC);

        DELETE FROM translation_memory;
        ALTER TABLE translation_memory DROP CONSTRAINT translation_memory_pkey;
        ALTER TABLE translation_memory ADD COLUMN owner_id UUID NOT NULL REFERENCES users(id);
        ALTER TABLE translation_memory ADD PRIMARY KEY (owner_id, src_hash, source_lang, target_lang);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE translation_memory DROP CONSTRAINT translation_memory_pkey;
        ALTER TABLE translation_memory DROP COLUMN owner_id;
        ALTER TABLE translation_memory ADD PRIMARY KEY (src_hash, source_lang, target_lang);

        DROP INDEX IF EXISTS ix_jobs_owner_created;
        ALTER TABLE translation_jobs DROP COLUMN owner_id;

        DROP INDEX IF EXISTS uq_documents_owner_sha256;
        CREATE INDEX ix_documents_sha256 ON documents (sha256);
        DROP INDEX IF EXISTS ix_documents_owner_created;
        ALTER TABLE documents DROP COLUMN owner_id;

        DROP TABLE IF EXISTS api_tokens;
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS users;
        """
    )
