"""plans_allow_multi_per_city

一个城市允许**多份**规划，让"保存到规划"能区分「新建」和「覆盖原规划」。

原来 (user_id, adcode) 是唯一索引 → 同一城市只可能有一条，set_plan 只能静默覆盖。
用户提的需求是"保存到规划可以增加新建规划和覆盖原规划的功能"，所以把唯一索引换成普通索引，
具体是新建还是覆盖由接口的 mode 决定。

Revision ID: c41a7f5d9e30
Revises: 8e1c4d7a2b93
Create Date: 2026-09-15

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c41a7f5d9e30'
down_revision: Union[str, Sequence[str], None] = '8e1c4d7a2b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """唯一索引 → 普通索引（同城可以有多份规划）

    ⚠️ 顺序不能改：MySQL 里 `user_id` 上有外键（→ users.id），而外键要求"以 user_id
    为最左列的索引"存在。idx_user_adcode(user_id, adcode) 正好满足，直接 drop 会报
        (1553, "Cannot drop index 'idx_user_adcode': needed in a foreign key constraint")
    所以先补一条 user_id 单列索引把外键的索引需求接过去，再换掉复合索引。
    """
    op.create_index('ix_plans_user_id', 'plans', ['user_id'], unique=False)
    op.drop_index('idx_user_adcode', table_name='plans')
    op.create_index('idx_user_adcode', 'plans', ['user_id', 'adcode'], unique=False)


def downgrade() -> None:
    """回到唯一索引。

    注意：**要求同一城市没有多份**，否则建唯一索引会失败（得先手工删掉多余的）。
    """
    op.drop_index('idx_user_adcode', table_name='plans')
    op.create_index('idx_user_adcode', 'plans', ['user_id', 'adcode'], unique=True)
    op.drop_index('ix_plans_user_id', table_name='plans')
