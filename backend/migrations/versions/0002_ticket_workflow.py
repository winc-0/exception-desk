"""工单号、状态流转、乐观锁和审计事件

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_POLICY = """
    CREATE POLICY tenant_isolation ON {table}
      USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
"""


def upgrade() -> None:
    # 迁移账号是表 owner，受 FORCE RLS 约束，看不到任何租户的数据。回填存量数据前要临时解除。
    op.execute("ALTER TABLE tickets NO FORCE ROW LEVEL SECURITY")

    op.execute("ALTER TABLE tickets DROP CONSTRAINT tickets_status_valid")
    op.execute(
        """
        ALTER TABLE tickets
          ADD COLUMN number integer,
          ADD COLUMN version integer NOT NULL DEFAULT 1,
          ADD COLUMN resolution text NOT NULL DEFAULT '',
          ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
          ADD CONSTRAINT tickets_status_valid CHECK (
            status IN ('open', 'in_progress', 'waiting', 'resolved', 'closed', 'cancelled')
          )
        """
    )
    op.execute(
        """
        UPDATE tickets t SET number = ranked.number, updated_at = t.created_at
        FROM (
          SELECT id,
                 row_number() OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS number
          FROM tickets
        ) ranked
        WHERE t.id = ranked.id
        """
    )
    op.execute("ALTER TABLE tickets ALTER COLUMN number SET NOT NULL")
    op.execute("DROP INDEX tickets_tenant_recent")
    op.execute("ALTER TABLE tickets ADD CONSTRAINT tickets_number_key UNIQUE (tenant_id, number)")
    op.execute("ALTER TABLE tickets ADD CONSTRAINT tickets_tenant_id_key UNIQUE (tenant_id, id)")
    # 由数据库保证：同一运单的同类异常只能有一张未关闭的工单，并发创建和重复导入都绕不过去
    op.execute(
        """
        CREATE UNIQUE INDEX tickets_one_active_per_shipment
          ON tickets (tenant_id, tracking_number, type)
          WHERE tracking_number IS NOT NULL AND status NOT IN ('closed', 'cancelled')
        """
    )

    op.execute(
        """
        CREATE TABLE ticket_counters (
          tenant_id uuid PRIMARY KEY REFERENCES tenants (id),
          last_number integer NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO ticket_counters (tenant_id, last_number)
        SELECT tenant_id, max(number) FROM tickets GROUP BY tenant_id
        """
    )

    op.execute(
        """
        CREATE TABLE ticket_events (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          tenant_id uuid NOT NULL,
          ticket_id uuid NOT NULL,
          actor_id uuid NOT NULL,
          kind text NOT NULL CONSTRAINT ticket_events_kind_valid
            CHECK (kind IN ('created', 'status_changed', 'assigned', 'commented')),
          from_status text,
          to_status text,
          body text NOT NULL DEFAULT '',
          data jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ticket_events_ticket_in_tenant FOREIGN KEY (tenant_id, ticket_id)
            REFERENCES tickets (tenant_id, id),
          CONSTRAINT ticket_events_actor_in_tenant FOREIGN KEY (tenant_id, actor_id)
            REFERENCES memberships (tenant_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX ticket_events_ticket ON ticket_events (tenant_id, ticket_id, id)")
    op.execute(
        """
        INSERT INTO ticket_events (tenant_id, ticket_id, actor_id, kind, to_status, created_at)
        SELECT tenant_id, id, created_by, 'created', status, created_at FROM tickets
        """
    )

    for table in ("ticket_counters", "ticket_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(_POLICY.format(table=table))
    op.execute("ALTER TABLE tickets FORCE ROW LEVEL SECURITY")

    op.execute("GRANT SELECT, INSERT, UPDATE ON ticket_counters TO desk_app")
    # 审计事件只能追加：应用角色没有 UPDATE 和 DELETE 权限
    op.execute("GRANT SELECT, INSERT ON ticket_events TO desk_app")


def downgrade() -> None:
    op.execute("DROP TABLE ticket_events, ticket_counters")
    op.execute("ALTER TABLE tickets NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP INDEX tickets_one_active_per_shipment")
    op.execute("ALTER TABLE tickets DROP CONSTRAINT tickets_status_valid")
    op.execute("UPDATE tickets SET status = 'open'")
    op.execute(
        """
        ALTER TABLE tickets
          DROP CONSTRAINT tickets_tenant_id_key,
          DROP CONSTRAINT tickets_number_key,
          DROP COLUMN number,
          DROP COLUMN version,
          DROP COLUMN resolution,
          DROP COLUMN updated_at,
          ADD CONSTRAINT tickets_status_valid CHECK (status IN ('open'))
        """
    )
    op.execute(
        "CREATE INDEX tickets_tenant_recent ON tickets (tenant_id, created_at DESC, id DESC)"
    )
    op.execute("ALTER TABLE tickets FORCE ROW LEVEL SECURITY")
