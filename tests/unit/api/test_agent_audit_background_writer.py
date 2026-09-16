"""后台路径的审计写入：write_agent_audit 在无请求上下文时不再静默丢事件。

背景：write_agent_audit 读 request.headers（X-Correlation-ID 等），
后台线程/调度器/ORM 直写路径没有请求上下文 → RuntimeError → 事件丢失
（2026-09-16 多 Agent E2E 实测：seed 的 agent.created/agent_key.created
在审计表里完全缺席）。修复 = has_request_context 防御 + 请求头降级为 None。
"""

import uuid

import pytest

from app import create_app

BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    from models import db
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


def _count_events(app, workspace_id, event_type):
    from models import AgentAuditEvent, db
    with app.app_context():
        return db.session.query(AgentAuditEvent).filter_by(
            workspace_id=workspace_id, event_type=event_type).count()


def test_write_agent_audit_without_request_context_persists(_isolated_app):
    """无请求上下文（纯 app_context）：事件落库、无异常、请求头字段为空。"""
    from api.agent_common import write_agent_audit
    from models import AgentAuditEvent, db

    ws_id = 910001
    write_agent_audit(
        event_type='agent.created',
        actor_type='user',
        actor_id=1,
        target_type='agent',
        target_id=77,
        workspace_id=ws_id,
        payload={'name': 'background-agent'},
    )
    db.session.commit()

    row = db.session.query(AgentAuditEvent).filter_by(
        workspace_id=ws_id, event_type='agent.created').one()
    assert row.actor_type == 'user'
    assert row.target_type == 'agent'
    assert row.correlation_id is None
    assert row.request_id is None


def test_write_agent_audit_payload_fallbacks_without_request(_isolated_app):
    """payload 里的 correlation_id/request_id 仍优先生效（不依赖请求头）。"""
    from api.agent_common import write_agent_audit
    from models import AgentAuditEvent, db

    ws_id = 910002
    write_agent_audit(
        event_type='task.leased',
        actor_type='agent',
        actor_id=5,
        target_type='task',
        target_id=42,
        workspace_id=ws_id,
        payload={'correlation_id': 'corr-1', 'request_id': 'req-1',
                 'attempt_id': 'att_x', 'duration_ms': 120},
        risk_score=10,
    )
    db.session.commit()

    row = db.session.query(AgentAuditEvent).filter_by(workspace_id=ws_id).one()
    assert row.correlation_id == 'corr-1'
    assert row.request_id == 'req-1'


def test_write_agent_audit_inside_request_context_still_reads_headers(client, _isolated_app):
    """有请求上下文时行为不变：请求头仍被抓取（回归）。"""
    from api.agent_common import write_agent_audit
    from models import AgentAuditEvent, db
    from flask import has_request_context

    ws_id = 910003
    with _isolated_app.test_request_context('/', headers={'X-Correlation-ID': 'corr-req-9'}):
        assert has_request_context()
        write_agent_audit(
            event_type='agent.key_revealed_probe',
            actor_type='user', actor_id=1,
            target_type='agent_key', target_id='x',
            workspace_id=ws_id,
        )
    db.session.commit()
    row = db.session.query(AgentAuditEvent).filter_by(workspace_id=ws_id).one()
    assert row.correlation_id == 'corr-req-9'
