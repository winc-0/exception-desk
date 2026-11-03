"""AI 记录区分回复草稿和索赔依据

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_ai_drafts
          ADD COLUMN kind text NOT NULL DEFAULT 'reply'
            CONSTRAINT ticket_ai_drafts_kind_valid CHECK (kind IN ('reply', 'brief'))
        """
    )
    op.execute("ALTER TABLE ticket_ai_drafts DROP CONSTRAINT ticket_ai_drafts_source_valid")
    op.execute(
        "ALTER TABLE ticket_ai_drafts ADD CONSTRAINT ticket_ai_drafts_source_valid "
        "CHECK (source IN ('model', 'template', 'rules'))"
    )


def downgrade() -> None:
    # 迁移账号受 FORCE RLS 约束，清理存量数据前要临时解除
    op.execute("ALTER TABLE ticket_ai_drafts NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM ticket_ai_drafts WHERE kind = 'brief'")
    op.execute("ALTER TABLE ticket_ai_drafts FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ticket_ai_drafts DROP CONSTRAINT ticket_ai_drafts_source_valid")
    op.execute(
        "ALTER TABLE ticket_ai_drafts ADD CONSTRAINT ticket_ai_drafts_source_valid "
        "CHECK (source IN ('model', 'template'))"
    )
    op.execute("ALTER TABLE ticket_ai_drafts DROP COLUMN kind")
