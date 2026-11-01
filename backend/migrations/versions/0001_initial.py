"""租户、账号、成员和最小工单表，以及行级安全策略

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_TENANT_TABLES = ("memberships", "tickets")


def upgrade() -> None:
    # 角色带密码，不适合写进迁移，由部署脚本提前创建（docker/postgres/init-roles.sh）
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'desk_app') THEN
            RAISE EXCEPTION '缺少数据库角色 desk_app，请先创建';
          END IF;
        END $$
        """
    )

    op.execute(
        """
        CREATE TABLE tenants (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          slug text NOT NULL UNIQUE CONSTRAINT tenants_slug_format
            CHECK (slug ~ '^[a-z0-9-]{2,40}$'),
          name text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE users (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          email text NOT NULL UNIQUE CONSTRAINT users_email_lowercase
            CHECK (email = lower(email)),
          password_hash text NOT NULL,
          display_name text NOT NULL,
          is_active boolean NOT NULL DEFAULT true,
          session_version integer NOT NULL DEFAULT 1,
          failed_login_count integer NOT NULL DEFAULT 0,
          locked_until timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memberships (
          tenant_id uuid NOT NULL REFERENCES tenants (id),
          user_id uuid NOT NULL REFERENCES users (id),
          role text NOT NULL CONSTRAINT memberships_role_valid
            CHECK (role IN ('admin', 'supervisor', 'agent', 'warehouse')),
          is_active boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE tickets (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL REFERENCES tenants (id),
          type text NOT NULL CONSTRAINT tickets_type_valid
            CHECK (type IN ('damaged', 'lost', 'delayed', 'address_change', 'other')),
          title text NOT NULL,
          tracking_number text,
          description text NOT NULL DEFAULT '',
          status text NOT NULL DEFAULT 'open' CONSTRAINT tickets_status_valid
            CHECK (status IN ('open')),
          created_by uuid NOT NULL,
          assignee_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT tickets_creator_in_tenant FOREIGN KEY (tenant_id, created_by)
            REFERENCES memberships (tenant_id, user_id),
          CONSTRAINT tickets_assignee_in_tenant FOREIGN KEY (tenant_id, assignee_id)
            REFERENCES memberships (tenant_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX tickets_tenant_recent ON tickets (tenant_id, created_at DESC, id DESC)"
    )

    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # 默认情况下表 owner 不受 RLS 限制，FORCE 让 owner 也受限
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # 未设置租户时 NULLIF 得到 NULL，比较结果不为真，默认拒绝
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
              WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            """
        )

    # 最小权限：应用角色不能改租户，也不能物理删除业务数据
    op.execute("GRANT SELECT ON tenants TO desk_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON users, memberships, tickets TO desk_app")


def downgrade() -> None:
    op.execute("DROP TABLE tickets, memberships, users, tenants")
