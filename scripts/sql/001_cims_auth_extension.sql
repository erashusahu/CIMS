-- CIMS foundation additions for the existing database "Inventroy".
-- Prepared from live, read-only metadata inspection on 2026-10-31.
-- This file has NOT been executed against PostgreSQL.
--
-- Adds three authentication tables and two columns to cims.app_user.
-- Reuses cims.tenant, cims.app_user and cims.current_tenant_id().
-- Does not create duplicate business/user/location/audit tables in public.
-- Contains no INSERT, UPDATE, DELETE, DROP, role creation or application grants.
--
-- Run once as the schema owner in pgAdmin's Query Tool for "Inventroy".
-- All changes are in one transaction. On an error, run ROLLBACK and inspect it.
--
-- Backend integration is still required after this file is applied:
--   * Map ORM models and raw SQL to the existing cims schema and tenant_id keys.
--   * Include tenant_id when creating refresh-token history.
--   * Implement restricted pre-authentication/session lookup before tenant context.
--   * Use a separate NOSUPERUSER/NOBYPASSRLS runtime role and explicit grants.
--   * Reconcile Alembic history; do NOT stamp the old 0001_foundation migration.

BEGIN;

DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the existing Inventroy database before running this file';
    END IF;

    IF to_regclass('cims.tenant') IS NULL
       OR to_regclass('cims.app_user') IS NULL
       OR to_regprocedure('cims.current_tenant_id()') IS NULL THEN
        RAISE EXCEPTION 'The required existing cims foundation is missing';
    END IF;

    IF to_regclass('cims.auth_sessions') IS NOT NULL
       OR to_regclass('cims.auth_refresh_tokens') IS NOT NULL
       OR to_regclass('cims.auth_throttles') IS NOT NULL THEN
        RAISE EXCEPTION 'An auth table already exists; compare its definition before applying';
    END IF;
END;
$preflight$;

-- 1. One row per signed-in session. Store hashes, never raw refresh tokens.
CREATE TABLE cims.auth_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    user_id UUID NOT NULL,
    refresh_token_hash VARCHAR(64) NOT NULL UNIQUE,
    family_id UUID NOT NULL DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_rotated_at TIMESTAMPTZ,

    CONSTRAINT auth_sessions_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES cims.tenant (tenant_id) ON DELETE CASCADE,
    CONSTRAINT auth_sessions_user_fk
        FOREIGN KEY (tenant_id, user_id)
        REFERENCES cims.app_user (tenant_id, user_id) ON DELETE CASCADE,
    CONSTRAINT auth_sessions_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT auth_sessions_hash_check
        CHECK (refresh_token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT auth_sessions_expiry_check CHECK (expires_at > created_at)
);

CREATE INDEX ix_auth_sessions_tenant_user
    ON cims.auth_sessions (tenant_id, user_id);
CREATE INDEX ix_auth_sessions_family
    ON cims.auth_sessions (family_id);
CREATE INDEX ix_auth_sessions_expiry
    ON cims.auth_sessions (expires_at);

-- 2. Retain rotated-token hashes so reuse can be detected and sessions revoked.
-- The composite foreign key prevents attaching a token to another tenant's session.
CREATE TABLE cims.auth_refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at TIMESTAMPTZ,

    CONSTRAINT auth_refresh_tokens_session_fk
        FOREIGN KEY (tenant_id, session_id)
        REFERENCES cims.auth_sessions (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT auth_refresh_tokens_hash_check
        CHECK (token_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_auth_refresh_tokens_session
    ON cims.auth_refresh_tokens (tenant_id, session_id);

-- 3. Pre-login rate-limit state. The identity/IP combination is stored as a hash.
-- This table is global because registration/login occurs before tenant authentication.
-- It is not exposed through a public API and receives no PUBLIC privileges.
CREATE TABLE cims.auth_throttles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope VARCHAR(20) NOT NULL,
    key_hash VARCHAR(64) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    blocked_until TIMESTAMPTZ,

    CONSTRAINT auth_throttles_scope_check CHECK (scope IN ('login', 'register')),
    CONSTRAINT auth_throttles_hash_check CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT auth_throttles_attempts_check CHECK (attempts >= 0),
    CONSTRAINT auth_throttles_scope_key UNIQUE (scope, key_hash)
);

CREATE INDEX ix_auth_throttles_window
    ON cims.auth_throttles (window_started_at);

-- Required staff-password fields missing from the existing user table.
-- Existing users retain unrestricted password status; new temporary accounts must
-- explicitly set password_change_required=true and an expiry in backend code.
ALTER TABLE cims.app_user
    ADD COLUMN password_change_required BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN temporary_password_expires_at TIMESTAMPTZ;

-- Match the existing cims tenant-context policy. Without authenticated transaction
-- context, ordinary roles cannot read or write these two tenant-owned tables.
ALTER TABLE cims.auth_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cims.auth_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cims.auth_sessions
    USING (tenant_id = cims.current_tenant_id())
    WITH CHECK (tenant_id = cims.current_tenant_id());

ALTER TABLE cims.auth_refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE cims.auth_refresh_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cims.auth_refresh_tokens
    USING (tenant_id = cims.current_tenant_id())
    WITH CHECK (tenant_id = cims.current_tenant_id());

REVOKE ALL ON TABLE cims.auth_sessions, cims.auth_refresh_tokens,
    cims.auth_throttles FROM PUBLIC;

COMMIT;
