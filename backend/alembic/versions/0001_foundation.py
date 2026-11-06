"""Phase 1 foundation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table("businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("identifier", sa.String(63), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("profile", sa.String(20), nullable=False), sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("country", sa.String(2), nullable=False, server_default="IN"), sa.Column("financial_year_start_month", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"), sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("permissions", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("key", sa.String(100), nullable=False, unique=True), sa.Column("description", sa.String(200), nullable=False))
    op.create_table("roles", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(40), nullable=False), sa.Column("system_role", sa.Boolean(), nullable=False, server_default=sa.true()), sa.UniqueConstraint("business_id", "name"))
    op.create_table("users", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("full_name", sa.String(200), nullable=False), sa.Column("email", sa.String(320), nullable=False), sa.Column("email_normalized", sa.String(320), nullable=False), sa.Column("password_hash", sa.Text(), nullable=False), sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id"), nullable=False), sa.Column("all_locations", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("password_change_required", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("temporary_password_expires_at", sa.DateTime(timezone=True)), sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("business_id", "email_normalized"))
    op.create_table("role_permissions", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False), sa.Column("permission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False), sa.UniqueConstraint("role_id", "permission_id"))
    op.create_table("locations", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("code", sa.String(40), nullable=False), sa.Column("code_normalized", sa.String(40), nullable=False), sa.Column("name", sa.String(150), nullable=False), sa.Column("type", sa.String(30), nullable=False), sa.Column("address", sa.Text()), sa.Column("city", sa.String(100)), sa.Column("state_code", sa.String(3)), sa.Column("pin_code", sa.String(10)), sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("business_id", "code_normalized"))
    op.create_table("user_location_access", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("location_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("locations.id", ondelete="CASCADE"), nullable=False), sa.UniqueConstraint("user_id", "location_id"))
    op.create_table("auth_sessions", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("refresh_token_hash", sa.String(64), nullable=False, unique=True), sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.Column("last_rotated_at", sa.DateTime(timezone=True)))
    op.create_table("auth_refresh_tokens", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("used_at", sa.DateTime(timezone=True)))
    op.create_index("ix_auth_refresh_session", "auth_refresh_tokens", ["session_id"])
    op.create_table("audit_logs", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False), sa.Column("actor_user_id", postgresql.UUID(as_uuid=True)), sa.Column("action", sa.String(100), nullable=False), sa.Column("affected_type", sa.String(80), nullable=False), sa.Column("affected_id", sa.String(80), nullable=False), sa.Column("request_id", sa.String(64), nullable=False), sa.Column("changes", postgresql.JSONB(), nullable=False, server_default="{}"), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("auth_throttles", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("scope", sa.String(20), nullable=False), sa.Column("key_hash", sa.String(64), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("window_started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("blocked_until", sa.DateTime(timezone=True)))
    op.execute("ALTER TABLE businesses ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation_businesses ON businesses USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON businesses TO cims_app")
    for table in ("roles", "users", "role_permissions", "locations", "user_location_access", "auth_sessions", "audit_logs"):
        op.create_index(f"ix_{table}_business_id", table, ["business_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation_{table} ON {table} USING (business_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (business_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.create_index("ix_auth_throttle_lookup", "auth_throttles", ["scope", "key_hash", "window_started_at"])
    op.execute("CREATE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit records are append-only'; END $$")
    op.execute("CREATE TRIGGER audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()")
    op.execute("CREATE FUNCTION reject_audit_truncate() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit records are append-only'; END $$")
    op.execute("CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_truncate()")
    op.execute("""CREATE FUNCTION cims_register_business(p_business_id uuid, p_identifier text, p_name text, p_profile text, p_user_id uuid, p_owner_name text, p_email text, p_email_normalized text, p_password_hash text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$ DECLARE r record; BEGIN
      PERFORM set_config('app.tenant_id',p_business_id::text,true);
      INSERT INTO businesses(id,identifier,name,profile) VALUES(p_business_id,p_identifier,p_name,p_profile);
      INSERT INTO permissions(id,key,description) VALUES
       (gen_random_uuid(),'business.read','Read business settings'),(gen_random_uuid(),'business.write','Edit business display name'),
       (gen_random_uuid(),'locations.manage','Manage locations'),(gen_random_uuid(),'staff.manage','Manage staff'),
       (gen_random_uuid(),'admin.grant','Grant or remove Admin access'),(gen_random_uuid(),'audit.read','Read audit history') ON CONFLICT(key) DO NOTHING;
      FOR r IN SELECT unnest(ARRAY['Owner','Admin','Warehouse Manager','Warehouse Staff','Sales','Procurement','Accountant','Viewer']) AS role_name LOOP
        INSERT INTO roles(id,business_id,name,system_role) VALUES(gen_random_uuid(),p_business_id,r.role_name,true);
      END LOOP;
      INSERT INTO users(id,business_id,full_name,email,email_normalized,password_hash,role_id,all_locations)
       SELECT p_user_id,p_business_id,p_owner_name,p_email,p_email_normalized,p_password_hash,id,true FROM roles WHERE business_id=p_business_id AND name='Owner';
      INSERT INTO audit_logs(id,business_id,actor_user_id,action,affected_type,affected_id,request_id,changes) VALUES(gen_random_uuid(),p_business_id,p_user_id,'business.registered','business',p_business_id::text,coalesce(current_setting('app.request_id',true),'registration'),jsonb_build_object('after',jsonb_build_object('name',p_name,'profile',p_profile)));
      INSERT INTO audit_logs(id,business_id,actor_user_id,action,affected_type,affected_id,request_id,changes) VALUES(gen_random_uuid(),p_business_id,p_user_id,'owner.created','user',p_user_id::text,coalesce(current_setting('app.request_id',true),'registration'),jsonb_build_object('email',p_email_normalized));
    END $$""")
    op.execute("""CREATE TABLE IF NOT EXISTS auth_refresh_tokens_guard (id integer primary key default 1 check (id=1))""")
    op.execute("""CREATE FUNCTION cims_auth_lookup(p_identifier text,p_email text) RETURNS TABLE(business_id uuid,business_suspended boolean,user_id uuid,password_hash text,user_suspended boolean,password_change_required boolean,temporary_password_expires_at timestamptz,session_role text) LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$ SELECT b.id,b.suspended,u.id,u.password_hash,u.suspended,u.password_change_required,u.temporary_password_expires_at,r.name FROM businesses b JOIN users u ON u.business_id=b.id JOIN roles r ON r.id=u.role_id WHERE b.identifier=p_identifier AND u.email_normalized=lower(p_email) LIMIT 1 $$""")
    op.execute("""CREATE FUNCTION cims_assign_roles() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$ BEGIN IF NEW.name='Owner' THEN INSERT INTO role_permissions(id,business_id,role_id,permission_id) SELECT gen_random_uuid(),NEW.business_id,NEW.id,p.id FROM permissions p; ELSIF NEW.name='Admin' THEN INSERT INTO role_permissions(id,business_id,role_id,permission_id) SELECT gen_random_uuid(),NEW.business_id,NEW.id,p.id FROM permissions p WHERE p.key IN ('business.read','business.write','locations.manage','staff.manage','audit.read'); END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER roles_assign_initial_permissions AFTER INSERT ON roles FOR EACH ROW EXECUTE FUNCTION cims_assign_roles()")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON auth_refresh_tokens TO cims_app")
    op.execute("REVOKE UPDATE,DELETE,TRUNCATE ON audit_logs FROM PUBLIC")
    op.execute("REVOKE UPDATE,DELETE,TRUNCATE ON audit_logs FROM cims_app")
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON users,roles,role_permissions,locations,user_location_access,auth_sessions,audit_logs,auth_throttles TO cims_app")
    op.execute("GRANT USAGE ON SCHEMA public TO cims_app")
    op.execute("GRANT EXECUTE ON FUNCTION cims_register_business(uuid,text,text,text,uuid,text,text,text,text) TO cims_app")
    op.execute("GRANT EXECUTE ON FUNCTION cims_auth_lookup(text,text) TO cims_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS cims_auth_lookup(text,text)")
    op.execute("DROP FUNCTION IF EXISTS cims_register_business(uuid,text,text,text,uuid,text,text,text,text)")
    op.execute("DROP FUNCTION IF EXISTS cims_assign_roles() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_truncate() CASCADE")
    for table in ("auth_throttles", "audit_logs", "auth_refresh_tokens", "auth_sessions", "user_location_access", "locations", "role_permissions", "users", "roles", "permissions", "businesses"):
        op.drop_table(table)
