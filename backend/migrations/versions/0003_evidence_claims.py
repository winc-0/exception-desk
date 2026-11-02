"""证据附件和承运商索赔

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_POLICY = """
    CREATE POLICY tenant_isolation ON {table}
      USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
"""

_OLD_KINDS = "'created', 'status_changed', 'assigned', 'commented'"
_NEW_KINDS = (
    f"{_OLD_KINDS}, 'attachment_added', 'attachment_voided', "
    "'claim_opened', 'claim_updated', 'claim_status_changed'"
)


def _set_event_kinds(kinds: str) -> None:
    op.execute("ALTER TABLE ticket_events DROP CONSTRAINT ticket_events_kind_valid")
    op.execute(
        "ALTER TABLE ticket_events ADD CONSTRAINT ticket_events_kind_valid "
        f"CHECK (kind IN ({kinds}))"
    )


def upgrade() -> None:
    _set_event_kinds(_NEW_KINDS)

    op.execute(
        """
        CREATE TABLE ticket_attachments (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL,
          ticket_id uuid NOT NULL,
          uploaded_by uuid NOT NULL,
          category text NOT NULL CONSTRAINT ticket_attachments_category_valid CHECK (
            category IN (
              'label_photo', 'package_photo', 'damage_photo', 'packing_video', 'weight_record',
              'tracking_screenshot', 'value_proof', 'customer_proof', 'other'
            )
          ),
          filename text NOT NULL,
          content_type text NOT NULL,
          size_bytes bigint NOT NULL CONSTRAINT ticket_attachments_size_positive
            CHECK (size_bytes > 0),
          sha256 text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          voided_at timestamptz,
          voided_by uuid,
          void_reason text NOT NULL DEFAULT '',
          CONSTRAINT ticket_attachments_ticket_in_tenant FOREIGN KEY (tenant_id, ticket_id)
            REFERENCES tickets (tenant_id, id),
          CONSTRAINT ticket_attachments_uploader_in_tenant FOREIGN KEY (tenant_id, uploaded_by)
            REFERENCES memberships (tenant_id, user_id),
          CONSTRAINT ticket_attachments_voider_in_tenant FOREIGN KEY (tenant_id, voided_by)
            REFERENCES memberships (tenant_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ticket_attachments_ticket ON ticket_attachments "
        "(tenant_id, ticket_id, created_at)"
    )
    # 同一张工单不重复保存同一个文件，重试上传也不会多出一份
    op.execute(
        """
        CREATE UNIQUE INDEX ticket_attachments_one_per_content
          ON ticket_attachments (tenant_id, ticket_id, sha256)
          WHERE voided_at IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE ticket_claims (
          tenant_id uuid NOT NULL,
          ticket_id uuid NOT NULL,
          version integer NOT NULL DEFAULT 1,
          status text NOT NULL DEFAULT 'preparing' CONSTRAINT ticket_claims_status_valid CHECK (
            status IN ('preparing', 'submitted', 'approved', 'rejected', 'paid', 'withdrawn')
          ),
          carrier text NOT NULL,
          claim_amount_cents bigint CONSTRAINT ticket_claims_claim_amount_positive
            CHECK (claim_amount_cents > 0),
          approved_cents bigint CONSTRAINT ticket_claims_approved_positive
            CHECK (approved_cents > 0),
          paid_cents bigint CONSTRAINT ticket_claims_paid_positive CHECK (paid_cents > 0),
          insured boolean NOT NULL DEFAULT false,
          pickup_weight_g integer CONSTRAINT ticket_claims_pickup_weight_positive
            CHECK (pickup_weight_g > 0),
          billed_weight_g integer CONSTRAINT ticket_claims_billed_weight_positive
            CHECK (billed_weight_g > 0),
          external_ref text NOT NULL DEFAULT '',
          rejection_reason text NOT NULL DEFAULT '',
          created_by uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          submitted_at timestamptz,
          paid_at timestamptz,
          PRIMARY KEY (tenant_id, ticket_id),
          CONSTRAINT ticket_claims_ticket_in_tenant FOREIGN KEY (tenant_id, ticket_id)
            REFERENCES tickets (tenant_id, id),
          CONSTRAINT ticket_claims_creator_in_tenant FOREIGN KEY (tenant_id, created_by)
            REFERENCES memberships (tenant_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX ticket_claims_status ON ticket_claims (tenant_id, status)")

    for table in ("ticket_attachments", "ticket_claims"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(_POLICY.format(table=table))

    # 证据内容不可修改，也不能删除：应用角色只能写作废相关的三列，原文件和记录都保留
    op.execute("GRANT SELECT, INSERT ON ticket_attachments TO desk_app")
    op.execute("GRANT UPDATE (voided_at, voided_by, void_reason) ON ticket_attachments TO desk_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ticket_claims TO desk_app")


def downgrade() -> None:
    op.execute("DROP TABLE ticket_claims, ticket_attachments")
    # 迁移账号受 FORCE RLS 约束，清理存量事件前要临时解除
    op.execute("ALTER TABLE ticket_events NO FORCE ROW LEVEL SECURITY")
    op.execute(f"DELETE FROM ticket_events WHERE kind NOT IN ({_OLD_KINDS})")
    _set_event_kinds(_OLD_KINDS)
    op.execute("ALTER TABLE ticket_events FORCE ROW LEVEL SECURITY")
