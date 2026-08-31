"""项目知识策展端点（P3.2 项目知识库自动策展）

- GET  /projects/<id>/knowledge-proposals?status=  提案列表（项目成员可看）
- POST /knowledge-proposals/<id>/confirm           人工确认 → 项目知识条目（管理权限）
- POST /knowledge-proposals/<id>/dismiss           驳回（管理权限）
"""

from flask import Blueprint, request

from models import Project, ProjectKnowledgeProposal, db
from core.auth import get_current_user, unified_auth_required
from .base import ApiResponse, validate_json_request
from .agent_common import write_agent_audit
from services.knowledge_curation import confirm_proposal, dismiss_proposal

knowledge_curation_bp = Blueprint('knowledge_curation', __name__)


def _get_project_or_404(project_id: int):
    project = db.session.get(Project, project_id)
    if not project:
        return None, ApiResponse.not_found('Project not found').to_response()
    return project, None


def _get_proposal_or_404(proposal_id: int):
    proposal = db.session.get(ProjectKnowledgeProposal, proposal_id)
    if not proposal:
        return None, ApiResponse.not_found('Knowledge proposal not found').to_response()
    return proposal, None


@knowledge_curation_bp.route('/projects/<int:project_id>/knowledge-proposals', methods=['GET'])
@unified_auth_required
def list_proposals(project_id: int):
    user = get_current_user()
    project, not_found = _get_project_or_404(project_id)
    if not_found:
        return not_found
    if not user.can_access_project(project):
        return ApiResponse.forbidden('Access denied').to_response()

    from .base import get_request_args

    args = get_request_args()
    page = max(args['page'], 1)
    per_page = min(max(args['per_page'], 1), 100)

    query = ProjectKnowledgeProposal.query.filter_by(project_id=project_id)
    status = request.args.get('status')
    if status:
        if status not in ProjectKnowledgeProposal.STATUSES:
            return ApiResponse.error(
                f'invalid status, one of {list(ProjectKnowledgeProposal.STATUSES)}', 400
            ).to_response()
        query = query.filter_by(status=status)
    query = query.order_by(ProjectKnowledgeProposal.id.desc())

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return ApiResponse.success(data={
        'items': [row.to_dict() for row in items],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'has_prev': page > 1,
            'has_next': page * per_page < total,
        },
    }).to_response()


@knowledge_curation_bp.route('/knowledge-proposals/<int:proposal_id>/confirm', methods=['POST'])
@unified_auth_required
def confirm(proposal_id: int):
    user = get_current_user()
    proposal, not_found = _get_proposal_or_404(proposal_id)
    if not_found:
        return not_found
    project, not_found = _get_project_or_404(proposal.project_id)
    if not_found:
        return not_found
    if not user.can_manage_project(project):
        return ApiResponse.forbidden('Access denied').to_response()

    data = validate_json_request(optional_fields=['title', 'content'])
    if isinstance(data, tuple):
        return data

    try:
        entry = confirm_proposal(
            proposal, user,
            title=data.get('title'), content=data.get('content'),
        )
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()

    write_agent_audit(
        event_type='knowledge.proposal_confirmed',
        actor_type='user',
        actor_id=user.id,
        target_type='knowledge_proposal',
        target_id=proposal.id,
        workspace_id=proposal.workspace_id,
        payload={'knowledge_entry_id': entry.id, 'project_id': proposal.project_id},
        risk_score=5,
    )
    return ApiResponse.success(
        data={'proposal': proposal.to_dict(), 'knowledge_entry': entry.to_dict()},
        message='Proposal confirmed and curated into project knowledge',
    ).to_response()


@knowledge_curation_bp.route('/knowledge-proposals/<int:proposal_id>/dismiss', methods=['POST'])
@unified_auth_required
def dismiss(proposal_id: int):
    user = get_current_user()
    proposal, not_found = _get_proposal_or_404(proposal_id)
    if not_found:
        return not_found
    project, not_found = _get_project_or_404(proposal.project_id)
    if not_found:
        return not_found
    if not user.can_manage_project(project):
        return ApiResponse.forbidden('Access denied').to_response()

    data = {}
    if request.is_json and request.get_json():
        data = request.get_json()

    try:
        dismiss_proposal(proposal, user, reason=data.get('reason'))
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()

    write_agent_audit(
        event_type='knowledge.proposal_dismissed',
        actor_type='user',
        actor_id=user.id,
        target_type='knowledge_proposal',
        target_id=proposal.id,
        workspace_id=proposal.workspace_id,
        payload={'reason': (data.get('reason') or '')[:200]},
        risk_score=5,
    )
    return ApiResponse.success(
        data={'proposal': proposal.to_dict()},
        message='Proposal dismissed',
    ).to_response()
