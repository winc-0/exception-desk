"""拒赔原因分类、货物数量和 AI 回复草稿

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_POLICY = """
    CREATE POLICY tenant_isolation ON ticket_ai_drafts
      USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
"""

_OLD_KINDS = (
    "'created', 'status_changed', 'assigned', 'commented', "
    "'attachment_added', 'attachment_voided', "
    "'claim_opened', 'claim_updated', 'claim_status_changed'"
)
_NEW_KINDS = f"{_OLD_KINDS}, 'ai_drafted'"


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
        ALTER TABLE ticket_claims
          ADD COLUMN goods_quantity integer CONSTRAINT ticket_claims_goods_quantity_positive
            CHECK (goods_quantity > 0),
          ADD COLUMN rejection_code text CONSTRAINT ticket_claims_rejection_code_valid CHECK (
            rejection_code IN (
              'insufficient_materials', 'damage_not_evident', 'packaging_intact',
              'uninsured', 'other'
            )
          )
        """
    )
    op.execute(
        """
        CREATE TABLE ticket_ai_drafts (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          ticket_id uuid NOT NULL,
          requested_by uuid NOT NULL,
          status text NOT NULL DEFAULT 'generating' CONSTRAINT ticket_ai_drafts_status_valid
            CHECK (status IN ('generating', 'done')),
          source text CONSTRAINT ticket_ai_drafts_source_valid
            CHECK (source IN ('model', 'template')),
          model text NOT NULL DEFAULT '',
          prompt_version text NOT NULL,
          draft text NOT NULL DEFAULT '',
          fallback_reason text NOT NULL DEFAULT '',
          attempts integer NOT NULL DEFAULT 0,
          input_tokens integer NOT NULL DEFAULT 0,
          output_tokens integer NOT NULL DEFAULT 0,
          latency_ms integer NOT NULL DEFAULT 0,
          feedback text CONSTRAINT ticket_ai_drafts_feedback_valid
            CHECK (feedback IN ('used', 'edited', 'rejected')),
          created_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz,
          CONSTRAINT ticket_ai_drafts_ticket_in_tenant FOREIGN KEY (tenant_id, ticket_id)
            REFERENCES tickets (tenant_id, id),
          CONSTRAINT ticket_ai_drafts_requester_in_tenant FOREIGN KEY (tenant_id, requested_by)
            REFERENCES memberships (tenant_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ticket_ai_drafts_quota ON ticket_ai_drafts "
        "(tenant_id, requested_by, created_at)"
    )
    op.execute("ALTER TABLE ticket_ai_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ticket_ai_drafts FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY)
    op.execute("GRANT SELECT, INSERT, UPDATE ON ticket_ai_drafts TO desk_app")


def downgrade() -> None:
    op.execute("DROP TABLE ticket_ai_drafts")
    op.execute(
        """
        ALTER TABLE ticket_claims
          DROP COLUMN rejection_code,
          DROP COLUMN goods_quantity
        """
    )
    # 迁移账号受 FORCE RLS 约束，清理存量事件前要临时解除
    op.execute("ALTER TABLE ticket_events NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM ticket_events WHERE kind = 'ai_drafted'")
    _set_event_kinds(_OLD_KINDS)
    op.execute("ALTER TABLE ticket_events FORCE ROW LEVEL SECURITY")
