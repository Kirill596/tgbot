"""Store one editable screen per user."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('ui_message_id', sa.Integer(), nullable=True))
    op.add_column('outbox', sa.Column('kind', sa.String(length=16), nullable=False, server_default='message'))
    op.alter_column('outbox', 'kind', server_default=None)


def downgrade():
    op.drop_column('outbox', 'kind')
    op.drop_column('users', 'ui_message_id')
