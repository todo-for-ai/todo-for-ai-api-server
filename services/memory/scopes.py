"""记忆作用域：五维度定义、继承链构建与隔离守卫。

维度（优先级从高到低，越具体越先被采信）：
    session → project → agent → user → organization

隔离规则：
- 每条记忆强制归属一个组织（organization_id 硬租户边界）；
- user 作用域是 per-org 的个人记忆（用户在组织 A 写的记忆对组织 B 不可见）；
- 继承链只能由本模块的构造器产生（从 loop/task/agent 等已验证归属的
  实体推导），调用方无法手工拼出越权的 scope 组合。
"""

from models import MemoryScopeType


class MemoryScopeRef:
    """一个作用域引用：(scope_type, scope_id, organization_id)。"""

    __slots__ = ('scope_type', 'scope_id', 'organization_id')

    def __init__(self, scope_type, scope_id, organization_id):
        if not isinstance(scope_type, MemoryScopeType):
            raise TypeError('scope_type must be MemoryScopeType')
        if scope_id is None or organization_id is None:
            raise ValueError('scope_id and organization_id are required')
        self.scope_type = scope_type
        self.scope_id = int(scope_id)
        self.organization_id = int(organization_id)

    def as_tuple(self):
        return (self.scope_type.value, self.scope_id)


def chain_from_loop(loop) -> list:
    """从 GoalLoop 推导完整继承链：会话(循环)→项目→Agent→User→组织。

    空缺维度（如 loop.created_by 为空）自动跳过，绝不产生 None scope。
    """
    chain = []
    if loop.id:
        chain.append(MemoryScopeRef(MemoryScopeType.SESSION, loop.id, loop.workspace_id))
    if loop.project_id:
        chain.append(MemoryScopeRef(MemoryScopeType.PROJECT, loop.project_id, loop.workspace_id))
    if loop.agent_id:
        chain.append(MemoryScopeRef(MemoryScopeType.AGENT, loop.agent_id, loop.workspace_id))
    if loop.created_by:
        chain.append(MemoryScopeRef(MemoryScopeType.USER, loop.created_by, loop.workspace_id))
    if loop.workspace_id:
        chain.append(MemoryScopeRef(MemoryScopeType.ORGANIZATION, loop.workspace_id, loop.workspace_id))
    return chain


def chain_from_task(task, agent_id=None) -> list:
    """从任务推导继承链：任务→项目→组织（会话维度由调用方按需加）。"""
    chain = []
    workspace_id = task.project.organization_id if task.project else task.owner_id
    if task.project_id:
        chain.append(MemoryScopeRef(MemoryScopeType.PROJECT, task.project_id, workspace_id))
    if agent_id:
        chain.append(MemoryScopeRef(MemoryScopeType.AGENT, agent_id, workspace_id))
    if workspace_id:
        chain.append(MemoryScopeRef(MemoryScopeType.ORGANIZATION, workspace_id, workspace_id))
    return chain


def dedupe_key_of(title: str, content: str) -> str:
    """同作用域幂等去重键：规范化文本的 sha1。"""
    import hashlib

    normalized = ' '.join(f'{title or ""}\n{content or ""}'.split()).strip().lower()
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest()
