"""规划器瞬时故障退避（planner transient backoff）语义回归。

长跑大忌：LLM 供应商抖动（超时/5xx/限流）曾直接烧语义受阻预算
（stall_limit 默认 2），watchdog 每 5 分钟推进一次 → 一次约 10 分钟的
供应商故障就把 RUNNING 循环打成 STALLED，全程要人工逐个 resume。
现在瞬时故障按指数退避等待自愈（retry_after 之前推进请求直接跳过），
不消耗 stall_count；只有密钥/额度类硬故障与坏输出才照常计受阻。
"""

import uuid
from datetime import datetime, timedelta

import pytest

from app import create_app
from models import db, GoalLoop, GoalLoopStatus
from services.goal_loop import state_machine
from services.goal_loop.constants import planner_backoff_seconds


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def db_session(_isolated_app):
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture(autouse=True)
def _real_planner_env(monkeypatch):
    """绕开 scripted 规划器：直接在状态机命名空间打桩 LLM 规划器。"""
    monkeypatch.setenv('GOAL_LOOP_PLANNER', 'llm')


@pytest.fixture
def loop(db_session, user_factory, organization_factory, agent_factory, project_factory):
    user = user_factory()
    org = organization_factory(owner_id=user.id)
    agent = agent_factory(workspace_id=org.id, runner_enabled=True)
    project = project_factory(owner_id=user.id, organization_id=org.id)
    lp = GoalLoop(
        workspace_id=org.id,
        project_id=project.id,
        agent_id=agent.id,
        title=f"loop_{uuid.uuid4().hex[:6]}",
        goal_text="把目标做成",
        status=GoalLoopStatus.RUNNING,
        rounds_limit=10,
        stall_limit=2,
        created_by=user.id,
    )
    db_session.add(lp)
    db_session.commit()
    return lp


def _fail_decompose(monkeypatch, exc):
    calls = {'n': 0}

    def _raise(_loop):
        calls['n'] += 1
        raise exc

    monkeypatch.setattr(state_machine, 'call_decompose', _raise)
    return calls


# ── 退避调度纯函数 ──

def test_backoff_schedule_doubles_and_caps():
    assert planner_backoff_seconds(1) == 300
    assert planner_backoff_seconds(2) == 600
    assert planner_backoff_seconds(3) == 1200
    assert planner_backoff_seconds(4) == 2400
    assert planner_backoff_seconds(5) == 3600
    assert planner_backoff_seconds(50) == 3600


def test_transient_classification():
    is_t = state_machine._is_transient_planner_error
    # 调用层故障 → 瞬时
    assert is_t(RuntimeError('llm_failed: timeout after 30s'))
    assert is_t(RuntimeError('llm_failed: HTTP 502 Bad Gateway'))
    assert is_t(RuntimeError('llm_failed: rate limit exceeded'))
    # 密钥/额度类 → 硬故障（需要人工，立即计受阻）
    assert not is_t(RuntimeError('llm_failed: 401 Unauthorized'))
    assert not is_t(RuntimeError('llm_failed: insufficient quota'))
    assert not is_t(RuntimeError('llm_failed: Invalid API Key'))
    # 坏输出类 → 硬故障（链路已通，模型/提示不匹配）
    assert not is_t(RuntimeError('llm_bad_plan'))
    assert not is_t(RuntimeError('llm_bad_action: maybe'))


# ── 状态机语义 ──

def test_transient_failure_backs_off_without_stall(db_session, loop, monkeypatch):
    calls = _fail_decompose(monkeypatch, RuntimeError('llm_failed: upstream timeout'))
    result = state_machine.maybe_advance(loop.id)
    assert result['reason'] == 'planner_backoff'
    db_session.expire(loop)
    assert loop.status == GoalLoopStatus.RUNNING
    assert loop.stall_count == 0
    assert loop.transient_streak == 1
    assert loop.retry_after is not None
    wait = (loop.retry_after - datetime.utcnow()).total_seconds()
    assert 0 < wait <= 300
    assert calls['n'] == 1


def test_backoff_window_skips_planner(db_session, loop, monkeypatch):
    _fail_decompose(monkeypatch, RuntimeError('llm_failed: upstream timeout'))
    state_machine.maybe_advance(loop.id)
    calls = _fail_decompose(monkeypatch, RuntimeError('llm_failed: still down'))
    result = state_machine.maybe_advance(loop.id)
    assert result['reason'] == 'planner_backoff_wait'
    assert calls['n'] == 0
    db_session.expire(loop)
    assert loop.transient_streak == 1  # 窗口内不累计


def test_recovery_after_window_creates_task(db_session, loop, monkeypatch):
    _fail_decompose(monkeypatch, RuntimeError('llm_failed: upstream timeout'))
    state_machine.maybe_advance(loop.id)
    db_session.expire(loop)
    assert loop.transient_streak == 1

    # 供应商恢复（窗口快进到期 → 重新调规划器 → 成功拆解并派发）
    loop.retry_after = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    def _ok_steps(_loop):
        return [{'title': '第一步', 'content': '开干'}]

    monkeypatch.setattr(state_machine, 'call_decompose', _ok_steps)
    result = state_machine.maybe_advance(loop.id)
    assert result.get('advanced') is True
    db_session.expire(loop)
    assert loop.transient_streak == 0
    assert loop.retry_after is None
    assert loop.last_error is None


def test_sustained_transient_failure_falls_back_to_stalled(db_session, loop, monkeypatch):
    monkeypatch.setattr(state_machine, 'DEFAULT_PLANNER_TRANSIENT_LIMIT', 2)
    _fail_decompose(monkeypatch, RuntimeError('llm_failed: provider outage'))
    # 第 1 次：退避
    assert state_machine.maybe_advance(loop.id)['reason'] == 'planner_backoff'
    db_session.expire(loop)
    loop.retry_after = None  # 快进：窗口已过
    db_session.commit()
    # 第 2 次：达到容忍上限 → 回落语义受阻计数（stall_count=1 < stall_limit=2，未终态）
    result = state_machine.maybe_advance(loop.id)
    assert result['reason'] == 'stall_counted'
    db_session.expire(loop)
    assert loop.stall_count == 1
    assert loop.status == GoalLoopStatus.RUNNING
    # 第 3 次：受阻计满 → STALLED（人工出口保留）
    result = state_machine.maybe_advance(loop.id)
    assert result['reason'] == 'stalled'
    db_session.expire(loop)
    assert loop.status == GoalLoopStatus.STALLED


def test_hard_failure_consumes_stall_immediately(db_session, loop, monkeypatch):
    _fail_decompose(monkeypatch, RuntimeError('llm_bad_plan'))
    result = state_machine.maybe_advance(loop.id)
    assert result['reason'] == 'stall_counted'
    db_session.expire(loop)
    assert loop.stall_count == 1
    assert loop.transient_streak == 0
    assert loop.retry_after is None


def test_auth_failure_is_hard_not_transient(db_session, loop, monkeypatch):
    _fail_decompose(monkeypatch, RuntimeError('llm_failed: 401 Unauthorized'))
    result = state_machine.maybe_advance(loop.id)
    assert result['reason'] == 'stall_counted'
    db_session.expire(loop)
    assert loop.stall_count == 1
    assert loop.transient_streak == 0


def test_resume_resets_transient_state(db_session, loop, monkeypatch):
    _fail_decompose(monkeypatch, RuntimeError('llm_failed: blip'))
    state_machine.maybe_advance(loop.id)
    db_session.expire(loop)
    assert loop.transient_streak == 1
    state_machine.set_status(loop.id, GoalLoopStatus.RUNNING)
    db_session.expire(loop)
    assert loop.transient_streak == 0
    assert loop.retry_after is None
    assert loop.stall_count == 0
