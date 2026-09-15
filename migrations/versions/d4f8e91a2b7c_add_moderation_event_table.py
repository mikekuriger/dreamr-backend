"""add moderation_event table

Revision ID: d4f8e91a2b7c
Revises: c3e91a7f5d02
Create Date: 2026-09-08 00:00:00.000000

Stores system-initiated moderation.check() rejections for admin review.
System-initiated counterpart to content_report (user-initiated). Lets us
confirm real abuse, spot false positives worth retuning
moderation.SCORE_THRESHOLDS for, and — for hard_block rows (currently just
sexual/minors) — know immediately rather than only via the rotating log
file.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4f8e91a2b7c'
down_revision = 'c3e91a7f5d02'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'moderation_event',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('label', sa.String(length=32), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('categories', sa.JSON(), nullable=False),
        sa.Column('raw_scores', sa.JSON(), nullable=True),
        sa.Column('hard_block', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=True),
    )
    op.create_index('ix_moderation_event_user_id', 'moderation_event', ['user_id'])
    op.create_index('ix_moderation_event_label', 'moderation_event', ['label'])
    op.create_index('ix_moderation_event_hard_block', 'moderation_event', ['hard_block'])
    op.create_index('ix_moderation_event_created_at', 'moderation_event', ['created_at'])


def downgrade():
    op.drop_index('ix_moderation_event_created_at', table_name='moderation_event')
    op.drop_index('ix_moderation_event_hard_block', table_name='moderation_event')
    op.drop_index('ix_moderation_event_label', table_name='moderation_event')
    op.drop_index('ix_moderation_event_user_id', table_name='moderation_event')
    op.drop_table('moderation_event')
