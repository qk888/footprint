"""add_user_memories_table

Revision ID: 7d4f2b9e1c60
Revises: 0ce20b32c727
Create Date: 2026-09-13 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7d4f2b9e1c60'
down_revision: Union[str, Sequence[str], None] = '0ce20b32c727'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('user_memories',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('content', sa.String(length=500), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('create_time', sa.DateTime(), nullable=False, comment='创建时间'),
    sa.Column('update_time', sa.DateTime(), nullable=False, comment='更新时间'),
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_memories_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_memories'))
    )
    op.create_index('idx_memory_user', 'user_memories', ['user_id', 'create_time'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_memory_user', table_name='user_memories')
    op.drop_table('user_memories')
