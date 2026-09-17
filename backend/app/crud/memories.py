#导入会话工厂
from app.config.db_config import AsyncSession
#获取操作数据库的方法
from sqlalchemy import select, and_, delete, update, func
#导入记忆模型
from app.models.memory import UserMemory
#python时间模块
from datetime import datetime

#只有"偏好/事实"类记忆才做近义合并; 金额/城市/行程是业务事件流水, 内容天然相似但不是重复
NEAR_DUP_KINDS = ("manual", "chat")
#字符二元组的包含度阈值: 超过就认为两句话在说同一件事(用户改口也算)
NEAR_DUP_RATIO = 0.75

#标点/空白不参与比对, 避免"我喜欢靠窗的座位！"和"我喜欢靠窗的座位"被当成两条
_STRIP_CHARS = " \t\r\n，,。.！!？?、；;：:~～…“”\"'（）()【】[]《》<>-—_*"


#归一化: 去掉标点空白, 英文转小写
def _norm(text: str) -> str:
    return "".join(ch.lower() for ch in (text or "") if ch not in _STRIP_CHARS)


#字符二元组集合(中文没法分词, 用字组的重合度衡量"是不是同一件事")
def _bigrams(text: str) -> set[str]:
    s = _norm(text)
    if len(s) <= 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


#近义判定: 以较短一方为分母的包含度, 对"短句是长句一部分"也友好
def _similar(a: str, b: str) -> bool:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return False
    if _norm(a) == _norm(b):
        return True
    return len(ga & gb) / min(len(ga), len(gb)) >= NEAR_DUP_RATIO


class MemoryRepository:
    #效果同操作用户数据库
    def __init__(self, session: AsyncSession):
        self.session = session

    #把同类别内与 content 近义的"生效中"记忆标成 superseded(exclude_id 用来跳过自己)
    async def _supersede_similar(
        self, user_id: int, kind: str, content: str, exclude_id: int | None = None
    ):
        if kind not in NEAR_DUP_KINDS:
            return 0
        actives = await self.session.scalars(
            select(UserMemory).where(
                and_(
                    UserMemory.user_id == user_id,
                    UserMemory.kind == kind,
                    UserMemory.status == "active",
                )
            )
        )
        count = 0
        for old in actives.all():
            if exclude_id and old.id == exclude_id:
                continue
            if _similar(old.content, content):
                old.status = "superseded"
                old.update_time = datetime.now()
                count += 1
        return count

    #写入记忆
    #  返回 (记忆对象, action), action 用于给用户回话:
    #    created=新写入 / exists=这条早就有了(只刷了时间) / revived=说回旧说法, 把它复活了
    #    updated=写新的并让旧说法失效
    async def remember(
        self,
        user_id: int,
        kind: str,
        content: str,
        source: str = "event",
        needs_review: bool = False,
    ):
        async with self.session.begin():
            stmt = select(UserMemory).where(
                and_(
                    UserMemory.user_id == user_id,
                    UserMemory.kind == kind,
                    UserMemory.content == content,
                )
            )
            existing = await self.session.scalar(stmt)
            if existing:
                was_active = existing.status == "active"
                existing.source = source
                # 显式赋值: 只改 source 且值相同时 ORM 不发 UPDATE, 时间就刷不上去
                existing.update_time = datetime.now()
                # 重复写入不该把用户已确认的记忆打回待确认
                if not needs_review:
                    existing.needs_review = 0
                if not was_active:
                    existing.status = "active"
                # 说回旧说法: 把它的近义版本也标失效, 否则会出现两条近似记忆同时生效
                await self._supersede_similar(
                    user_id, kind, existing.content, exclude_id=existing.id
                )
                return existing, ("exists" if was_active else "revived")

            superseded = await self._supersede_similar(user_id, kind, content)

            memory = UserMemory(
                user_id=user_id,
                kind=kind,
                content=content,
                source=source,
                status="active",
                needs_review=1 if needs_review else 0,
            )
            self.session.add(memory)
            return memory, ("updated" if superseded else "created")

    #抹掉一条记忆（业务事件让旧记忆失效时用）, 返回删除条数
    async def forget(self, user_id: int, kind: str, content: str):
        async with self.session.begin():
            stmt = delete(UserMemory).where(
                and_(
                    UserMemory.user_id == user_id,
                    UserMemory.kind == kind,
                    UserMemory.content == content,
                )
            )
            result = await self.session.execute(stmt)
            return result.rowcount

    #抹掉某类下以指定开头的所有记忆（金额这类会变的内容要先清旧的）, 返回删除条数
    async def forget_prefix(self, user_id: int, kind: str, prefix: str):
        async with self.session.begin():
            stmt = delete(UserMemory).where(
                and_(
                    UserMemory.user_id == user_id,
                    UserMemory.kind == kind,
                    UserMemory.content.like(f"{prefix}%"),
                )
            )
            result = await self.session.execute(stmt)
            return result.rowcount

    #按关键词召回记忆（规则匹配, 不走向量）
    #只排除"已被取代"的; "待确认"的照样返回 —— 用户自己问起来当然要告诉他
    async def search(self, user_id: int, keyword: str, limit: int = 5):
        async with self.session.begin():
            like = f"%{keyword}%"
            stmt = (
                select(UserMemory)
                .where(
                    and_(
                        UserMemory.user_id == user_id,
                        UserMemory.content.like(like),
                        UserMemory.status == "active",
                    )
                )
                .order_by(UserMemory.update_time.desc())
                .limit(limit)
            )
            result = await self.session.scalars(stmt)
            return result.all()

    #取"可以注入提示词"的候选: 只取生效中, 默认还要用户已确认过(模型自己记的候选不自动带上)
    async def list_active(
        self,
        user_id: int,
        kinds: tuple[str, ...] | None = None,
        limit: int = 60,
        include_pending: bool = False,
        offset: int = 0,
    ):
        async with self.session.begin():
            conds = [UserMemory.user_id == user_id, UserMemory.status == "active"]
            if not include_pending:
                conds.append(UserMemory.needs_review == 0)
            if kinds:
                conds.append(UserMemory.kind.in_(kinds))
            stmt = (
                select(UserMemory)
                .where(and_(*conds))
                #同一批次写入的记忆 update_time 会完全相同, 不带 id 兜底的话分页会重复/漏条
                .order_by(UserMemory.update_time.desc(), UserMemory.id.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await self.session.scalars(stmt)
            return result.all()

    #统计"生效中"的记忆条数(含待确认): 用来决定要不要启用语义检索
    async def count_active(self, user_id: int) -> int:
        async with self.session.begin():
            stmt = select(func.count()).select_from(UserMemory).where(
                and_(UserMemory.user_id == user_id, UserMemory.status == "active")
            )
            return int(await self.session.scalar(stmt) or 0)

    #按 id 批量取"生效中"的记忆(语义召回后用, 回库核对状态 —— 向量库里的失效/删除记不过来)
    async def list_active_by_ids(
        self, user_id: int, ids: list[int], include_pending: bool = False
    ):
        if not ids:
            return []
        async with self.session.begin():
            conds = [
                UserMemory.user_id == user_id,
                UserMemory.status == "active",
                UserMemory.id.in_(ids),
            ]
            if not include_pending:
                conds.append(UserMemory.needs_review == 0)
            stmt = select(UserMemory).where(and_(*conds))
            result = await self.session.scalars(stmt)
            return result.all()

    #获取记忆列表（生效中的在前, 管理页要能看到全部含已失效）
    async def list_memories(self, user_id: int, limit: int = 100):
        async with self.session.begin():
            stmt = (
                select(UserMemory)
                .where(UserMemory.user_id == user_id)
                .order_by(UserMemory.status.asc(), UserMemory.update_time.desc())
                .limit(limit)
            )
            result = await self.session.scalars(stmt)
            return result.all()

    #获取单条记忆
    async def get_memory(self, user_id: int, memory_id: int):
        async with self.session.begin():
            stmt = select(UserMemory).where(
                and_(UserMemory.id == memory_id, UserMemory.user_id == user_id)
            )
            return await self.session.scalar(stmt)

    #确认一条"待确认"的模型记忆: 置为已确认, 之后才会自动注入
    async def confirm_memory(self, user_id: int, memory_id: int):
        async with self.session.begin():
            stmt = (
                update(UserMemory)
                .where(
                    and_(
                        UserMemory.id == memory_id,
                        UserMemory.user_id == user_id,
                        UserMemory.status == "active",
                    )
                )
                .values(needs_review=0, update_time=datetime.now())
            )
            result = await self.session.execute(stmt)
            return result.rowcount > 0

    #删除单条记忆
    async def delete_memory(self, user_id: int, memory_id: int):
        async with self.session.begin():
            stmt = select(UserMemory).where(
                and_(UserMemory.id == memory_id, UserMemory.user_id == user_id)
            )
            memory = await self.session.scalar(stmt)
            if memory:
                await self.session.delete(memory)
                return True
            return False

    #清空该用户全部记忆, 返回删除条数
    async def delete_all(self, user_id: int):
        async with self.session.begin():
            stmt = delete(UserMemory).where(UserMemory.user_id == user_id)
            result = await self.session.execute(stmt)
            return result.rowcount
