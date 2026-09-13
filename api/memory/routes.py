"""记忆管理路由——把记忆开放给用户编辑（Phase 2）。

挂载：/todo-for-ai/api/v1/memory

授权矩阵（读取=组织成员即可；写入按维度收紧）：
| 维度          | 创建/编辑/遗忘                                  |
|---------------|--------------------------------------------------|
| organization  | 组织 owner/admin（can_manage_organization）      |
| project       | 项目 owner/maintainer（can_manage_project）      |
| agent         | Agent 的 owner 或组织管理者                      |
| user          | 仅本人（scope_id == current_user.id）            |
| session       | 拒绝手工写入（系统管理的临时记忆）               |

所有读写在 organization_id 租户边界内进行：跨组织访问一律视为不存在
（404），不泄漏存在性。
"""

from flask import request

from models import db, Agent, Organization, Project, MemoryScopeType
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse
from . import memory_bp

from services.memory import store as memory_store
from services.memory.scopes import MemoryScopeRef

def _session_write_error():
    return ApiResponse.error(
        'session 记忆由系统管理（随循环运行生灭），不支持手工写入',
        400, error_details={'code': 'SESSION_SCOPE_SYSTEM_MANAGED'},
    ).to_response()


def _org_or_error(organization_id):
    """返回 (org, user, error_response)。读取与写入的租户边界入口。"""
    user = get_current_user()
    org = db.session.get(Organization, int(organization_id)) if organization_id else None
    if not org:
        return None, None, ApiResponse.error(
            'Organization not found', 404,
            error_details={'code': 'ORGANIZATION_NOT_FOUND'},
        ).to_response()
    if not user.can_access_organization(org):
        return None, None, ApiResponse.error(
            'Access denied', 403, error_details={'code': 'PERMISSION_DENIED'},
        ).to_response()
    return org, user, None


def _memory_or_error(memory_id):
    """返回 (memory, user, error)。按 ID 直查后校验成员资格；
    跨组织一律 404（不泄漏存在性）。"""
    user = get_current_user()
    row = memory_store.get_memory_any(memory_id)
    if row is None:
        return None, None, ApiResponse.error(
            'Memory not found', 404, error_details={'code': 'MEMORY_NOT_FOUND'},
        ).to_response()
    org = db.session.get(Organization, row.organization_id)
    if org is None or not user.can_access_organization(org):
        return None, None, ApiResponse.error(
            'Memory not found', 404, error_details={'code': 'MEMORY_NOT_FOUND'},
        ).to_response()
    return row, user, None


def _scope_write_allowed(user, org, scope_type: str, scope_id: int):
    """按维度判断写权限。返回 (MemoryScopeRef|None, error_response)。"""
    try:
        scope_enum = MemoryScopeType(scope_type)
    except ValueError:
        return None, ApiResponse.error(
            f'invalid scope_type: {scope_type}', 400,
            error_details={'code': 'INVALID_SCOPE_TYPE'},
        ).to_response()

    if scope_enum == MemoryScopeType.SESSION:
        return None, _session_write_error()

    if scope_enum == MemoryScopeType.ORGANIZATION:
        if int(scope_id) != int(org.id) or not user.can_manage_organization(org):
            return None, ApiResponse.error(
                '组织级记忆仅组织 owner/admin 可写', 403,
                error_details={'code': 'PERMISSION_DENIED'},
            ).to_response()
        return MemoryScopeRef(scope_enum, org.id, org.id), None

    if scope_enum == MemoryScopeType.PROJECT:
        project = db.session.get(Project, int(scope_id))
        if not project or project.organization_id != org.id:
            return None, ApiResponse.error(
                'Project not found in organization', 404,
                error_details={'code': 'PROJECT_NOT_FOUND'},
            ).to_response()
        if not user.can_manage_project(project):
            return None, ApiResponse.error(
                '项目级记忆仅项目 owner/maintainer 可写', 403,
                error_details={'code': 'PERMISSION_DENIED'},
            ).to_response()
        return MemoryScopeRef(scope_enum, project.id, org.id), None

    if scope_enum == MemoryScopeType.AGENT:
        agent = db.session.get(Agent, int(scope_id))
        if not agent or agent.workspace_id != org.id:
            return None, ApiResponse.error(
                'Agent not found in organization', 404,
                error_details={'code': 'AGENT_NOT_FOUND'},
            ).to_response()
        if agent.owner_id != user.id and not user.can_manage_organization(org):
            return None, ApiResponse.error(
                'Agent 级记忆仅 Agent 所有者或组织管理者可写', 403,
                error_details={'code': 'PERMISSION_DENIED'},
            ).to_response()
        return MemoryScopeRef(scope_enum, agent.id, org.id), None

    # user 维度：仅本人
    if int(scope_id) != int(user.id):
        return None, ApiResponse.error(
            'user 级记忆仅可写本人作用域', 403,
            error_details={'code': 'PERMISSION_DENIED'},
        ).to_response()
    return MemoryScopeRef(scope_enum, user.id, org.id), None


@memory_bp.route('', methods=['GET'])
@unified_auth_required
def list_memories():
    organization_id = request.args.get('organization_id', type=int)
    org, user, error = _org_or_error(organization_id)
    if error:
        return error
    result = memory_store.list_memories(
        organization_id=org.id,
        scope_type=request.args.get('scope_type') or None,
        scope_id=request.args.get('scope_id', type=int),
        kind=request.args.get('kind') or None,
        keyword=request.args.get('q') or None,
        include_invalid=request.args.get('include_invalid', default='false',
                                          ).strip().lower() in ('1', 'true', 'yes'),
        page=request.args.get('page', type=int) or 1,
        page_size=request.args.get('page_size', type=int) or 20,
    )
    return ApiResponse.success(result, 'Memories listed').to_response()


@memory_bp.route('', methods=['POST'])
@unified_auth_required
def create_memory():
    data = request.get_json(silent=True) or {}
    org, user, error = _org_or_error(data.get('organization_id'))
    if error:
        return error

    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    if not title or not content:
        return ApiResponse.error(
            'title and content are required', 400,
            error_details={'code': 'TITLE_CONTENT_REQUIRED'},
        ).to_response()

    scope_ref, error = _scope_write_allowed(
        user, org, str(data.get('scope_type') or ''), data.get('scope_id'))
    if error:
        return error

    result = memory_store.remember(
        scope_ref,
        kind=(data.get('kind') or 'insight').strip() or 'insight',
        title=title,
        content=content,
        source_type='human',
        agent_id=None,
        confidence=data.get('confidence', 90),
        human_edited=True,
    )
    payload = result['memory'].to_dict()
    payload['deduplicated'] = not result['created']
    return ApiResponse.success(
        payload, 'Memory created' if result['created'] else 'Memory already exists',
    ).to_response()


@memory_bp.route('/<int:memory_id>', methods=['PUT'])
@unified_auth_required
def edit_memory(memory_id):
    memory, user, error = _memory_or_error(memory_id)
    if error:
        return error
    org = db.session.get(Organization, memory.organization_id)

    scope_ref, werror = _scope_write_allowed(
        user, org, memory.scope_type, memory.scope_id)
    if werror:
        return werror

    data = request.get_json(silent=True) or {}
    result = memory_store.update_memory(
        memory.id, memory.organization_id,
        title=data.get('title'),
        content=data.get('content'),
        kind=data.get('kind'),
        confidence=data.get('confidence'),
    )
    return ApiResponse.success(
        result['memory'].to_dict(), 'Memory updated',
    ).to_response()


@memory_bp.route('/<int:memory_id>', methods=['DELETE'])
@unified_auth_required
def forget_memory(memory_id):
    memory, user, error = _memory_or_error(memory_id)
    if error:
        return error
    org = db.session.get(Organization, memory.organization_id)

    scope_ref, werror = _scope_write_allowed(
        user, org, memory.scope_type, memory.scope_id)
    if werror:
        return werror

    forgotten = memory_store.forget_by_id(memory.id, memory.organization_id)
    return ApiResponse.success(
        {'id': memory.id, 'forgotten': forgotten}, 'Memory forgotten',
    ).to_response()


@memory_bp.route('/recall', methods=['POST'])
@unified_auth_required
def recall_preview():
    """召回预览：给定作用域链输入，返回 agent 实际会看到的记忆（带维度标签）。"""
    data = request.get_json(silent=True) or {}
    org, user, error = _org_or_error(data.get('organization_id'))
    if error:
        return error

    query = (data.get('query') or '').strip()
    if not query:
        return ApiResponse.error(
            'query is required', 400, error_details={'code': 'QUERY_REQUIRED'},
        ).to_response()

    chain = [MemoryScopeRef(MemoryScopeType.ORGANIZATION, org.id, org.id)]
    if data.get('project_id'):
        project = db.session.get(Project, int(data['project_id']))
        if project and project.organization_id == org.id:
            chain.insert(0, MemoryScopeRef(
                MemoryScopeType.PROJECT, project.id, org.id))
    if data.get('agent_id'):
        agent = db.session.get(Agent, int(data['agent_id']))
        if agent and agent.workspace_id == org.id:
            chain.insert(0, MemoryScopeRef(
                MemoryScopeType.AGENT, agent.id, org.id))
    if data.get('loop_id'):
        from models import GoalLoop

        loop = db.session.get(GoalLoop, int(data['loop_id']))
        if loop and loop.workspace_id == org.id:
            chain.insert(0, MemoryScopeRef(
                MemoryScopeType.SESSION, loop.id, org.id))

    top_k = data.get('top_k') or 5
    hits = memory_store.recall(chain, query, top_k=top_k)
    return ApiResponse.success(
        {'query': query, 'hits': hits}, 'Recall preview',
    ).to_response()


@memory_bp.route('/<int:memory_id>', methods=['GET'])
@unified_auth_required
def get_memory_detail(memory_id):
    memory, user, error = _memory_or_error(memory_id)
    if error:
        return error
    return ApiResponse.success(memory.to_dict(), 'Memory detail').to_response()
