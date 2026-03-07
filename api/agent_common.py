"""
Agent API 公共工具
"""

import hmac
import hashlib
import os
import secrets
from datetime import datetime
from functools import wraps
from typing import Any, Optional
from flask import g, request
from models import (
    db,
    Agent,
    AgentSession,
    AgentAuditEvent,
    Organization,
)
from .base import ApiResponse


def now_utc():
    return datetime.utcnow()


def generate_id(prefix):
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def sign_link_payload(payload):
    secret = os.environ.get('AGENT_LINK_SIGNING_SECRET') or os.environ.get('SECRET_KEY', 'todo-for-ai-agent-secret')
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return digest


def get_workspace_or_404(workspace_id):
    workspace = Organization.query.get(workspace_id)
    if not workspace:
        return None, ApiResponse.not_found('Workspace not found').to_response()
    return workspace, None


def ensure_workspace_access(user, workspace):
    if not user or not user.can_access_organization(workspace):
        return ApiResponse.forbidden('Access denied').to_response()
    return None


def ensure_agent_manage_access(user, agent):
    if not user:
        return ApiResponse.forbidden('Access denied').to_response()

    if agent.creator_user_id == user.id:
        return None

    role = user.get_organization_role(agent.workspace)
    if role in {'owner', 'admin'}:
        return None

    return ApiResponse.forbidden('Access denied').to_response()


def _to_int_optional(raw_value: Any) -> Optional[int]:
    if raw_value in (None, ''):
        return None
    try:
        return int(str(raw_value).strip())
    except Exception:
        return None


def _to_text_optional(raw_value: Any) -> Optional[str]:
    text = str(raw_value or '').strip()
    return text or None


def _derive_audit_level(risk_score: int, payload: dict) -> str:
    explicit = str(payload.get('audit_level') or payload.get('level') or '').strip().lower()
    if explicit in {'info', 'warn', 'error'}:
        return explicit
    if risk_score >= 50:
        return 'error'
    if risk_score >= 20:
        return 'warn'
    return 'info'


def write_agent_audit(event_type, actor_type, actor_id, target_type, target_id, workspace_id, payload=None, risk_score=0):
    payload_data = dict(payload or {}) if isinstance(payload, dict) else {}
    if payload and not isinstance(payload, dict):
        payload_data['raw_payload'] = str(payload)

    source = str(payload_data.get('audit_source') or payload_data.get('source') or 'api').strip().lower() or 'api'
    correlation_id = _to_text_optional(
        payload_data.get('correlation_id')
        or payload_data.get('trace_id')
        or request.headers.get('X-Correlation-ID')
        or request.headers.get('X-Trace-ID')
    )
    request_id = _to_text_optional(
        payload_data.get('request_id')
        or request.headers.get('X-Request-ID')
    )
    run_id = _to_text_optional(payload_data.get('run_id'))
    attempt_id = _to_text_optional(payload_data.get('attempt_id'))
    task_id = _to_int_optional(payload_data.get('task_id'))
    project_id = _to_int_optional(payload_data.get('project_id'))
    duration_ms = _to_int_optional(payload_data.get('duration_ms'))
    error_code = _to_text_optional(payload_data.get('error_code'))

    actor_agent_id = _to_int_optional(payload_data.get('actor_agent_id'))
    if actor_agent_id is None and str(actor_type or '').strip().lower() == 'agent':
        actor_agent_id = _to_int_optional(actor_id)

    target_agent_id = _to_int_optional(payload_data.get('target_agent_id'))
    if target_agent_id is None and str(target_type or '').strip().lower() == 'agent':
        target_agent_id = _to_int_optional(target_id)

    event = AgentAuditEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=str(actor_id),
        target_type=target_type,
        target_id=str(target_id),
        source=source,
        level=_derive_audit_level(int(risk_score or 0), payload_data),
        risk_score=risk_score,
        correlation_id=correlation_id,
        request_id=request_id,
        run_id=run_id,
        attempt_id=attempt_id,
        task_id=task_id,
        project_id=project_id,
        actor_agent_id=actor_agent_id,
        target_agent_id=target_agent_id,
        duration_ms=duration_ms,
        error_code=error_code,
        payload=payload_data,
        ip=request.remote_addr,
        user_agent=request.headers.get('User-Agent', ''),
        occurred_at=now_utc(),
    )
    db.session.add(event)


def agent_session_required(f):
    """Agent 运行时鉴权：Bearer <agent_session_token>"""

    @wraps(f)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return ApiResponse.unauthorized('Missing or invalid authorization header').to_response()

        raw_token = auth_header.split(' ', 1)[1]
        session = AgentSession.verify_session_token(raw_token)
        if not session:
            return ApiResponse.unauthorized('Invalid or expired agent session token').to_response()

        agent = Agent.query.get(session.agent_id)
        if not agent:
            return ApiResponse.unauthorized('Agent is inactive').to_response()

        status_value = agent.status.value if hasattr(agent.status, 'value') else str(agent.status).lower()
        if status_value != 'active':
            return ApiResponse.unauthorized('Agent is inactive').to_response()

        g.current_agent = agent
        g.current_agent_session = session
        return f(*args, **kwargs)

    return wrapped
