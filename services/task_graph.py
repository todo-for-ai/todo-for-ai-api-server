"""任务图（DAG）服务：依赖环检测 + 项目任务图构建。

有向图语义：blocked_by 存的是「本任务依赖哪些任务」，边方向 blocker → task
（阻塞者先完成，下游才派发——见 agent_runtime_pull 的依赖门）。环 = 依赖门
下互相等待、永久无法派发：写侧必须拒绝成环编辑（update_dependencies），
规划侧丢弃成环边（goal_decomposition），读侧把环显式暴露（task-graph 端点）。
"""

from models import db, Task, TaskStatus

# 单项目任务图规模上限：超过则截断并标记 truncated（防御性，图布局与
# 环检测都按有限图设计）
GRAPH_NODE_LIMIT = 500


def normalize_dependency_ids(raw_ids):
    """容忍脏数据地解析依赖 id 列表：非数字项丢弃、去重保序。

    与派发依赖门（agent_runtime_pull._unsatisfied_blocker_ids）同一容错口径。
    """
    seen = set()
    ids = []
    for raw in raw_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def blocked_by_map_for(task_ids):
    """批量取一组任务的 blocked_by（归一化后），返回 {task_id: [blocker_id]}。"""
    if not task_ids:
        return {}
    rows = (
        db.session.query(Task.id, Task.blocked_by_task_ids)
        .filter(Task.id.in_(task_ids))
        .all()
    )
    return {int(row[0]): normalize_dependency_ids(row[1]) for row in rows}


def find_dependency_cycle(task_id, new_blocker_ids):
    """检查「把 new_blocker_ids 加为 task_id 的依赖」是否成环。

    新边方向是 blocker → task。成环条件：从任一新阻塞者沿 blocked_by 链
    （逐级前置）能走回 task——此时新边恰好闭合一个有向环。自依赖同拦。
    返回成环路径上碰到的那个节点 id；无环返回 None。
    """
    blocker_set = set(normalize_dependency_ids(new_blocker_ids))
    target = int(task_id)
    if target in blocker_set:
        return target

    visited = set()
    frontier = list(blocker_set)
    while frontier:
        current = frontier.pop()
        if current == target:
            return current
        if current in visited:
            continue
        visited.add(current)
        frontier.extend(blocked_by_map_for([current]).get(current, []))
    return None


def cyclic_groups(edges_by_node):
    """Tarjan SCC（迭代实现），返回有向图中成环的节点组。

    成环组 = 大小 > 1 的强连通分量，或含自环的单节点。
    edges_by_node: {node_id: [后继节点 id]}——本模块语义下后继即 blocked_by。
    """
    index_counter = [0]
    indices, lowlink = {}, {}
    stack, on_stack = [], set()
    groups = []

    for root in edges_by_node:
        if root in indices:
            continue
        work = [(root, 0)]
        while work:
            node, next_i = work[-1]
            if next_i == 0:
                indices[node] = lowlink[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            neighbors = edges_by_node.get(node, [])
            recursed = False
            for i in range(next_i, len(neighbors)):
                nxt = neighbors[i]
                if nxt not in indices:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    recursed = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], indices[nxt])
            if recursed:
                continue
            work.pop()
            if lowlink[node] == indices[node]:
                group = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                if len(group) > 1 or node in neighbors:
                    groups.append(sorted(group))
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return groups


def build_project_task_graph(project_id):
    """构建项目任务图：节点（含就绪态）、边、环组、统计。

    readiness 与派发依赖门同语义：
    - done / cancelled：终态，不再派发；
    - blocked：存在未解除依赖（阻塞者未到终态）；
    - ready：无未解除依赖，可被派发（还受租约/预算/窗口等其余门约束）。
    指向已删除任务的失效引用按已解除计（与派发门一致），但保留在
    blocked_by 里原样透出。
    """
    rows = (
        db.session
        .query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.id.desc())
        .limit(GRAPH_NODE_LIMIT + 1)
        .all()
    )
    truncated = len(rows) > GRAPH_NODE_LIMIT
    tasks = rows[:GRAPH_NODE_LIMIT]
    id_set = {t.id for t in tasks}

    per_task_deps = {t.id: normalize_dependency_ids(t.blocked_by_task_ids) for t in tasks}
    referenced = set()
    for ids in per_task_deps.values():
        referenced.update(ids)

    status_by_id = {}
    if referenced:
        for row in db.session.query(Task.id, Task.status).filter(Task.id.in_(referenced)).all():
            status_by_id[int(row[0])] = row[1]

    terminal = (TaskStatus.DONE, TaskStatus.CANCELLED)

    def _satisfied(blocker_id):
        status = status_by_id.get(blocker_id)
        # 失效引用（任务已删）视为解除，不卡下游
        return status is None or status in terminal

    nodes = []
    edges = []
    edges_by_node = {}
    for t in tasks:
        deps = per_task_deps[t.id]
        unresolved = [b for b in deps if not _satisfied(b)]
        if t.status in terminal:
            readiness = 'done' if t.status == TaskStatus.DONE else 'cancelled'
        elif unresolved:
            readiness = 'blocked'
        else:
            readiness = 'ready'
        in_project = [b for b in deps if b in id_set]
        edges_by_node[t.id] = in_project
        for blocker_id in in_project:
            edges.append({'from': blocker_id, 'to': t.id})
        nodes.append({
            'id': t.id,
            'title': t.title,
            'status': t.status.value if t.status else None,
            'readiness': readiness,
            'priority': t.priority.value if t.priority else None,
            'is_ai_task': bool(t.is_ai_task),
            'epic_id': t.epic_id,
            'assignees': t.assignees or [],
            'blocked_by': deps,
            'unresolved_blockers': unresolved,
        })

    groups = cyclic_groups(edges_by_node)
    stats = {'total': len(nodes), 'ready': 0, 'blocked': 0, 'done': 0, 'cancelled': 0}
    for node in nodes:
        stats[node['readiness']] += 1

    return {
        'project_id': project_id,
        'nodes': nodes,
        'edges': edges,
        'cycles': groups,
        'stats': stats,
        'truncated': truncated,
    }
