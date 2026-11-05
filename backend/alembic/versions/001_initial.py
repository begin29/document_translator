"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-11-05
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE documents (
            id UUID PRIMARY KEY,
            filename VARCHAR(255) NOT NULL,
            format VARCHAR(16) NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            storage_path TEXT NOT NULL,
            page_count INTEGER,
            char_count INTEGER,
            detected_source_lang VARCHAR(16),
            analysis_status VARCHAR(32) NOT NULL DEFAULT 'queued',
            analysis JSONB,
            error_code VARCHAR(64),
            error_detail TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_documents_sha256 ON documents (sha256);
        CREATE INDEX ix_documents_analysis_status ON documents (analysis_status);

        CREATE TABLE blocks (
            id UUID PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            page INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            kind VARCHAR(32) NOT NULL,
            text TEXT,
            src_hash VARCHAR(64) NOT NULL,
            asset_path TEXT,
            meta JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (document_id, idx)
        );
        CREATE INDEX ix_blocks_document ON blocks (document_id);

        CREATE TABLE translation_jobs (
            id UUID PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(id),
            source_lang VARCHAR(16),
            target_lang VARCHAR(16) NOT NULL,
            model VARCHAR(64) NOT NULL,
            segment_budget_tokens INTEGER,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            attempt INTEGER NOT NULL DEFAULT 0,
            locked_by VARCHAR(64),
            locked_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            total_segments INTEGER NOT NULL DEFAULT 0,
            done_segments INTEGER NOT NULL DEFAULT 0,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            cost_cents NUMERIC(14, 4) NOT NULL DEFAULT 0,
            error_code VARCHAR(64),
            error_detail TEXT,
            output_path TEXT,
            output_stale BOOLEAN NOT NULL DEFAULT false,
            idempotency_key VARCHAR(128) UNIQUE,
            summary TEXT,
            glossary JSONB,
            render_mode VARCHAR(16) NOT NULL DEFAULT 'reflow',
            quality_warning BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_jobs_status_created ON translation_jobs (status, created_at);
        CREATE INDEX ix_jobs_document ON translation_jobs (document_id);

        CREATE TABLE segments (
            id UUID PRIMARY KEY,
            job_id UUID NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            block_start INTEGER NOT NULL,
            block_end INTEGER NOT NULL,
            model VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            locked_by VARCHAR(64),
            locked_at TIMESTAMPTZ,
            review_state VARCHAR(32),
            UNIQUE (job_id, idx)
        );
        CREATE INDEX ix_segments_job_status ON segments (job_id, status);

        CREATE TABLE block_translations (
            job_id UUID NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
            block_idx INTEGER NOT NULL,
            tgt_text TEXT NOT NULL,
            source VARCHAR(16) NOT NULL,
            segment_id UUID,
            PRIMARY KEY (job_id, block_idx)
        );

        CREATE TABLE translation_memory (
            src_hash VARCHAR(64) NOT NULL,
            source_lang VARCHAR(16) NOT NULL,
            target_lang VARCHAR(16) NOT NULL,
            tgt_text TEXT NOT NULL,
            model VARCHAR(64) NOT NULL,
            hits INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (src_hash, source_lang, target_lang)
        );

        CREATE TABLE job_events (
            id SERIAL PRIMARY KEY,
            job_id UUID NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            level VARCHAR(16) NOT NULL DEFAULT 'info',
            event VARCHAR(64) NOT NULL,
            payload JSONB
        );
        CREATE INDEX ix_job_events_job ON job_events (job_id, id);

        CREATE TABLE provider_calls (
            id SERIAL PRIMARY KEY,
            job_id UUID,
            segment_id UUID,
            page_no INTEGER,
            stage VARCHAR(32) NOT NULL,
            model VARCHAR(64) NOT NULL,
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            attempt INTEGER NOT NULL DEFAULT 1,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_cached INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            cost_cents NUMERIC(14, 4) NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(16) NOT NULL,
            error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_provider_calls_job ON provider_calls (job_id, stage);

        CREATE TABLE ocr_pages (
            job_id UUID NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
            page_no INTEGER NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            locked_by VARCHAR(64),
            locked_at TIMESTAMPTZ,
            PRIMARY KEY (job_id, page_no)
        );

        CREATE TABLE provider_gate (
            id INTEGER PRIMARY KEY,
            inflight INTEGER NOT NULL DEFAULT 0,
            max_inflight INTEGER NOT NULL DEFAULT 0,
            effective_max INTEGER NOT NULL DEFAULT 32,
            ceiling INTEGER NOT NULL DEFAULT 32,
            tpm_budget INTEGER NOT NULL DEFAULT 2000000,
            window_tokens INTEGER NOT NULL DEFAULT 0,
            window_start TIMESTAMPTZ,
            last_429_at TIMESTAMPTZ,
            last_ramp_at TIMESTAMPTZ
        );
        INSERT INTO provider_gate (id, effective_max, ceiling, tpm_budget) VALUES (1, 32, 32, 2000000);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS provider_gate;
        DROP TABLE IF EXISTS ocr_pages;
        DROP TABLE IF EXISTS provider_calls;
        DROP TABLE IF EXISTS job_events;
        DROP TABLE IF EXISTS translation_memory;
        DROP TABLE IF EXISTS block_translations;
        DROP TABLE IF EXISTS segments;
        DROP TABLE IF EXISTS translation_jobs;
        DROP TABLE IF EXISTS blocks;
        DROP TABLE IF EXISTS documents;
        """
    )
