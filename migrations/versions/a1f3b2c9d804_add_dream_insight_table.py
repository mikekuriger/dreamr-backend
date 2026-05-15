"""add dream_insight table

Revision ID: a1f3b2c9d804
Revises: c8a91e2d4b6f
Create Date: 2026-05-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1f3b2c9d804'
down_revision = 'c8a91e2d4b6f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dream_insight',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('generated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('window_end', sa.DateTime(), nullable=False),
        sa.Column('dream_count', sa.Integer(), nullable=False),
        sa.Column('narrative', sa.Text(), nullable=False),
        sa.Column('symbols', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('themes', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('patterns', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('questions', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('prompt_version', sa.Integer(), nullable=False, server_default='1'),
    )
    op.create_index(
        'ix_dream_insight_user_generated',
        'dream_insight',
        ['user_id', 'generated_at'],
    )
    op.create_index(
        'ix_dream_insight_user_id',
        'dream_insight',
        ['user_id'],
    )


def downgrade():
    op.drop_index('ix_dream_insight_user_id', table_name='dream_insight')
    op.drop_index('ix_dream_insight_user_generated', table_name='dream_insight')
    op.drop_table('dream_insight')
