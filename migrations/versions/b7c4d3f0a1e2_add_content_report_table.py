"""add content_report table

Revision ID: b7c4d3f0a1e2
Revises: a1f3b2c9d804
Create Date: 2026-05-20 00:00:00.000000

Stores user-submitted reports flagging AI-generated content. Required by
Google Play's AI-Generated Content policy: users must be able to report
offensive AI output without exiting the app, and developers must keep
records to inform moderation.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7c4d3f0a1e2'
down_revision = 'a1f3b2c9d804'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'content_report',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('content_type', sa.String(length=32), nullable=False),
        sa.Column('content_id', sa.String(length=64), nullable=True),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('content_snapshot', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=True),
    )
    op.create_index('ix_content_report_user_id', 'content_report', ['user_id'])
    op.create_index('ix_content_report_content_id', 'content_report', ['content_id'])
    op.create_index('ix_content_report_created_at', 'content_report', ['created_at'])


def downgrade():
    op.drop_index('ix_content_report_created_at', table_name='content_report')
    op.drop_index('ix_content_report_content_id', table_name='content_report')
    op.drop_index('ix_content_report_user_id', table_name='content_report')
    op.drop_table('content_report')
