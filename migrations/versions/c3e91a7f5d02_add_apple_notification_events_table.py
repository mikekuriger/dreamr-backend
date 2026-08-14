"""add apple_notification_events table

Revision ID: c3e91a7f5d02
Revises: b7c4d3f0a1e2
Create Date: 2026-08-14 00:00:00.000000

Idempotency ledger for App Store Server Notifications V2
(apple_notifications.handle_notification). notification_uuid is Apple's
own retry-safe dedup key: notification_uuid is unique so a redelivered
retry of the same event is rejected by the DB even under a race, not just
by the pre-insert application-level check.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3e91a7f5d02'
down_revision = 'b7c4d3f0a1e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'apple_notification_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('notification_uuid', sa.String(length=36), nullable=False),
        sa.Column('notification_type', sa.String(length=50), nullable=False),
        sa.Column('subtype', sa.String(length=50), nullable=True),
        sa.Column('transaction_id', sa.String(length=100), nullable=True),
        sa.Column('original_transaction_id', sa.String(length=100), nullable=True),
        sa.Column('signed_date', sa.DateTime(), nullable=True),
        sa.Column('processed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'ix_apple_notification_events_notification_uuid',
        'apple_notification_events', ['notification_uuid'], unique=True,
    )
    op.create_index(
        'ix_apple_notification_events_transaction_id',
        'apple_notification_events', ['transaction_id'],
    )
    op.create_index(
        'ix_apple_notification_events_original_transaction_id',
        'apple_notification_events', ['original_transaction_id'],
    )


def downgrade():
    op.drop_index('ix_apple_notification_events_original_transaction_id', table_name='apple_notification_events')
    op.drop_index('ix_apple_notification_events_transaction_id', table_name='apple_notification_events')
    op.drop_index('ix_apple_notification_events_notification_uuid', table_name='apple_notification_events')
    op.drop_table('apple_notification_events')
