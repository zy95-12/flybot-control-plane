"""Desired runtime state and short-lived owner launch handoff."""
from alembic import op
import sqlalchemy as sa
revision = '0002_runtime'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('runtime_state',
        sa.Column('instance_id', sa.String(36), sa.ForeignKey('instances.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('desired', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('observed_revision', sa.Integer(), nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('heartbeat_at', sa.BigInteger(), nullable=False))
    op.create_table('runtime_launch',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('instance_id', sa.String(36), sa.ForeignKey('instances.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_hash', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('launch_url', sa.Text()))
    op.create_index('ix_runtime_launch_expiry', 'runtime_launch', ['expires_at'])


def downgrade():
    op.drop_table('runtime_launch')
    op.drop_table('runtime_state')
