"""add memory status and needs_review

Revision ID: 8e1c4d7a2b93
Revises: 7d4f2b9e1c60
Create Date: 2026-09-14 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8e1c4d7a2b93'
down_revision: Union[str, Sequence[str], None] = '7d4f2b9e1c60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 老数据一律视为"生效中 + 已确认", 行为不变
    op.add_column(
        'user_memories',
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
    )
    op.add_column(
        'user_memories',
        sa.Column('needs_review', sa.SmallInteger(), nullable=False, server_default='0'),
    )
    op.create_index(
        'idx_memory_user_status', 'user_memories', ['user_id', 'status'], unique=False
    )


def downgrade() -> None:
    op.drop_index('idx_memory_user_status', table_name='user_memories')
    op.drop_column('user_memories', 'needs_review')
    op.drop_column('user_memories', 'status')
