"""Durable administrator upgrade requests."""
from alembic import op
import sqlalchemy as sa
revision = '0003_upgrades'
down_revision = '0002_runtime'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('upgrade_jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('phase', sa.String(32), nullable=False),
        sa.Column('commit', sa.String(40)),
        sa.Column('version', sa.String(64)),
        sa.Column('completed', sa.Integer(), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False))
    op.create_index('ix_upgrade_jobs_created', 'upgrade_jobs', ['created_at'])


def downgrade():
    op.drop_index('ix_upgrade_jobs_created', table_name='upgrade_jobs')
    op.drop_table('upgrade_jobs')
