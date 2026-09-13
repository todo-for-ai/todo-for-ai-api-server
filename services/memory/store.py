"""作用域化记忆存取（builtin 存储：agent_memories 表）。

- remember：写一条记忆（同作用域 dedupe_key 幂等；命中即视为已存在，
  顺带刷新置信度——重复验证的记忆更可信）；
- recall：沿继承链按优先级召回（session → project → agent → user →
  organization），每条命中带维度标签；全部查询强制 organization_id 过滤
  （硬租户边界）+ is_valid=1；
- forget：按作用域软删（is_valid=0）。

匹配算法复用 builtin 后端的 CJK 二元切分（中文无空格分词）。
"""

import logging
from datetime import datetime

from models import AgentMemory, MemoryScopeType, SCOPE_PRECEDENCE, db

from .builtin import _keywords, _snippet
from .scopes import dedupe_key_of

log = logging.getLogger(__name__)


def remember(scope, kind, title, content, *, source_type='manual',
             source_task_id=None, agent_id=None, confidence=70,
             human_edited=False) -> dict:
    """写一条作用域记忆（幂等）。返回 {'memory': row, 'created': bool}。"""
    key = dedupe_key_of(title, content)
    existing = AgentMemory.query.filter_by(
        organization_id=scope.organization_id,
        scope_type=scope.scope_type.value,
        scope_id=scope.scope_id,
        dedupe_key=key,
    ).first()
    if existing:
        # 重复验证：置信度小幅上调（封顶 95）；人工来源补标 human_edited
        existing.confidence = min(95, int(existing.confidence or 0) + 5)
        if human_edited:
            existing.human_edited = 1
        db.session.commit()
        return {'memory': existing, 'created': False}

    row = AgentMemory(
        organization_id=scope.organization_id,
        scope_type=scope.scope_type.value,
        scope_id=scope.scope_id,
        kind=kind,
        title=(title or '')[:500],
        content=(content or '').strip(),
        source_type=source_type,
        source_task_id=source_task_id,
        agent_id=agent_id,
        confidence=max(0, min(100, int(confidence))),
        human_edited=1 if human_edited else 0,
        dedupe_key=key,
        created_by=f'system:memory:{source_type}',
    )
    db.session.add(row)
    db.session.commit()
    return {'memory': row, 'created': True}


def recall(scope_chain, query, top_k=3, per_scope=2):
    """沿继承链召回：按维度优先级合并，命中带标签，总长可控。

    隔离：MemoryScopeRef 自带 organization_id，查询永远限单一组织。
    """
    if not (query or '').strip() or not scope_chain:
        return []

    keywords = _keywords(query)
    if not keywords:
        return []

    by_scope = {}
    for ref in scope_chain:
        by_scope.setdefault(ref.scope_type, []).append(ref)

    hits = []
    for scope_type in SCOPE_PRECEDENCE:
        refs = by_scope.get(scope_type)
        if not refs:
            continue
        for ref in refs:
            hits.extend(_recall_scope(ref, keywords, per_scope))
        if len(hits) >= top_k * 2:
            break

    # 维度优先级为主序（session=0 最前）、置信度为次序；同维度内保持查询顺序
    hits.sort(key=lambda h: (h['_precedence'], -h['confidence']))
    return [
        {
            'scope_label': h['row'].scope_label,
            'title': h['row'].title,
            'snippet': h['snippet'],
        }
        for h in hits[:top_k]
    ]


def _recall_scope(ref, keywords, per_scope):
    rows = (
        AgentMemory.query.filter(
            AgentMemory.organization_id == ref.organization_id,
            AgentMemory.scope_type == ref.scope_type.value,
            AgentMemory.scope_id == ref.scope_id,
            AgentMemory.is_valid == 1,
        )
        .order_by(AgentMemory.confidence.desc(), AgentMemory.id.desc())
        .limit(200)
        .all()
    )
    matched = []
    for row in rows:
        haystack = f'{row.title}\n{row.content}'.lower()
        score = sum(1 for kw in keywords if kw in haystack)
        if score > 0:
            matched.append({
                'row': row,
                'snippet': _snippet(row.content, keywords),
                # 人工创建/编辑过的记忆可信度更高（+10 排序加权）
                'confidence': (
                    int(row.confidence or 0)
                    + (10 if row.human_edited else 0)
                    + score * 5
                ),
                '_precedence': list(SCOPE_PRECEDENCE).index(
                    MemoryScopeType(row.scope_type)
                ),
            })
    matched.sort(key=lambda h: -h['confidence'])
    results = matched[:per_scope]

    # 召回计数（学习信号）；失败不阻断
    try:
        for item in results:
            item['row'].access_count = (item['row'].access_count or 0) + 1
            item['row'].last_accessed_at = datetime.utcnow()
        if results:
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return results


def forget(scope, keyword=None) -> int:
    """软删某作用域下的记忆（keyword 过滤可选）。返回失效条数。"""
    filters = [
        AgentMemory.organization_id == scope.organization_id,
        AgentMemory.scope_type == scope.scope_type.value,
        AgentMemory.scope_id == scope.scope_id,
        AgentMemory.is_valid == 1,
    ]
    rows = AgentMemory.query.filter(*filters).all()
    changed = 0
    for row in rows:
        if keyword and keyword.lower() not in f'{row.title}\n{row.content}'.lower():
            continue
        row.is_valid = 0
        changed += 1
    if changed:
        db.session.commit()
    return changed


def format_memory_lines(hits) -> list:
    """召回结果格式化为注入行（带维度标签）。"""
    lines = []
    for h in hits:
        line = f"- [{h['scope_label']}] {h['title']}"
        if h['snippet']:
            line += f"：{h['snippet']}"
        lines.append(line)
    return lines


# ── 用户开放编辑（REST API 背后的存取层） ──────────────────────────────


def list_memories(organization_id, scope_type=None, scope_id=None, kind=None,
                  keyword=None, include_invalid=False, page=1, page_size=20) -> dict:
    """分页列出组织内记忆（租户边界内；用户编辑界面的数据源）。"""
    from sqlalchemy import or_

    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    filters = [AgentMemory.organization_id == int(organization_id)]
    if not include_invalid:
        filters.append(AgentMemory.is_valid == 1)
    if scope_type:
        filters.append(AgentMemory.scope_type == str(scope_type))
    if scope_id is not None:
        filters.append(AgentMemory.scope_id == int(scope_id))
    if kind:
        filters.append(AgentMemory.kind == str(kind))
    if keyword and str(keyword).strip():
        kw = f'%{str(keyword).strip()}%'
        filters.append(or_(
            AgentMemory.title.ilike(kw),
            AgentMemory.content.ilike(kw),
        ))

    q = AgentMemory.query.filter(*filters).order_by(AgentMemory.id.desc())
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    return {
        'items': [r.to_dict() for r in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
    }


def get_memory(memory_id, organization_id):
    """按 ID 取记忆（强制租户边界；跨组织视为不存在）。"""
    return AgentMemory.query.filter_by(
        id=int(memory_id), organization_id=int(organization_id),
    ).first()


def get_memory_any(memory_id):
    """按 ID 直查（不带租户过滤——调用方必须自行校验组织成员资格）。"""
    return db.session.get(AgentMemory, int(memory_id))


def update_memory(memory_id, organization_id, *, title=None, content=None,
                  kind=None, confidence=None) -> dict:
    """人工编辑一条记忆（幂等去重键随新内容重算；标记 human_edited）。"""
    row = get_memory(memory_id, organization_id)
    if row is None or not row.is_valid:
        return {'memory': None, 'updated': False}

    if title is not None:
        row.title = str(title).strip()[:500] or row.title
    if content is not None:
        new_content = str(content).strip()
        if new_content:
            row.content = new_content
    if kind is not None and str(kind).strip():
        row.kind = str(kind).strip()
    if confidence is not None:
        row.confidence = max(0, min(100, int(confidence)))
    row.dedupe_key = dedupe_key_of(row.title, row.content)
    row.human_edited = 1
    db.session.commit()
    return {'memory': row, 'updated': True}


def forget_by_id(memory_id, organization_id) -> bool:
    """按 ID 软删（遗忘）。跨组织/已失效返回 False。"""
    row = get_memory(memory_id, organization_id)
    if row is None or not row.is_valid:
        return False
    row.is_valid = 0
    db.session.commit()
    return True
