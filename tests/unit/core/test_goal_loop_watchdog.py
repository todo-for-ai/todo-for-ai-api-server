"""GoalLoop 看门狗调度器（core/goal_loop_watchdog.py）回归。

门控开关、间隔解析（下限/非法值回退）、sweep 主循环（含 Pod 回收汇总、
异常吞噬继续）、线程启停幂等——调度循环本体此前 0 覆盖。
"""

import logging
import threading

import pytest
from flask import Flask

from core import goal_loop_watchdog as wd


@pytest.fixture(autouse=True)
def _reset_globals():
    """模块级线程/状态在用例间互不污染。"""
    wd._scheduler_thread = None
    wd._stop_event = threading.Event()
    wd._last_run = None
    yield
    # 测试可能替换过 _stop_event（如 _TwoStepEvent），先恢复真 Event
    wd._stop_event = threading.Event()
    wd._stop_event.set()
    thread = wd._scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    wd._scheduler_thread = None
    wd._last_run = None


@pytest.fixture
def app():
    return Flask(__name__)


class _TwoStepEvent:
    """第一次 wait 放行（进循环体），第二次置位（退出循环）。"""

    def __init__(self):
        self.calls = 0

    def set(self):
        pass

    def wait(self, timeout):
        self.calls += 1
        return self.calls > 1


class TestGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("GOAL_LOOP_WATCHDOG_ENABLED", raising=False)
        assert wd.enabled() is False

    def test_enabled_truthy_values(self, monkeypatch):
        for value in ("1", "true", "Yes", "ON", " on "):
            monkeypatch.setenv("GOAL_LOOP_WATCHDOG_ENABLED", value)
            assert wd.enabled() is True, value

    def test_enabled_rejects_falsy_values(self, monkeypatch):
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("GOAL_LOOP_WATCHDOG_ENABLED", value)
            assert wd.enabled() is False, value

    def test_interval_default(self, monkeypatch):
        monkeypatch.delenv("GOAL_LOOP_WATCHDOG_INTERVAL_SECONDS", raising=False)
        assert wd.interval_seconds() == 300

    def test_interval_custom_and_floor(self, monkeypatch):
        monkeypatch.setenv("GOAL_LOOP_WATCHDOG_INTERVAL_SECONDS", "120")
        assert wd.interval_seconds() == 120
        monkeypatch.setenv("GOAL_LOOP_WATCHDOG_INTERVAL_SECONDS", "5")
        assert wd.interval_seconds() == 30  # 下限 30s

    def test_interval_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("GOAL_LOOP_WATCHDOG_INTERVAL_SECONDS", "abc")
        assert wd.interval_seconds(default=77) == 77


class TestWatchdogLoop:
    def _run_one_cycle(self, monkeypatch, app, sweep_result=None, sweep_exc=None,
                       recycle_result=None, recycle_exc=None):
        """以假事件同步驱动循环恰好一轮。"""
        from unittest.mock import patch as mock_patch

        fake_event = _TwoStepEvent()
        monkeypatch.setattr(wd, "_stop_event", fake_event)

        if sweep_exc:
            sweep = lambda: (_ for _ in ()).throw(sweep_exc)  # noqa: E731
        else:
            sweep = lambda: dict(sweep_result)  # noqa: E731
        recycle = None
        if recycle_exc:
            recycle = lambda _c: (_ for _ in ()).throw(recycle_exc)  # noqa: E731
        else:
            recycle = lambda _c: dict(recycle_result or {"checked": 2, "recycled": 1})

        with mock_patch("services.goal_loop_service.watchdog_sweep", sweep), \
                mock_patch("services.workspace_runtime_policy.recycle_idle_pods", recycle), \
                mock_patch("services.agent_runtime_controller.get_agent_controller",
                           lambda: object()):
            wd._watchdog_loop(app, interval=0)

    def test_sweep_records_summary_with_pod_recycle(self, monkeypatch, app):
        self._run_one_cycle(
            monkeypatch, app,
            sweep_result={"time_exhausted": 0, "stuck_cancelled": 0, "kicked": 0})
        run = wd.last_run()
        assert run is not None
        assert run["summary"]["pods_checked"] == 2
        assert run["summary"]["pods_recycled"] == 1
        assert run["finished_at"]

    def test_sweep_activity_logged(self, monkeypatch, app, caplog):
        caplog.set_level(logging.INFO, logger="core.goal_loop_watchdog")
        self._run_one_cycle(
            monkeypatch, app,
            sweep_result={"time_exhausted": 0, "stuck_cancelled": 0, "kicked": 3})
        assert any("GOAL_LOOP_WATCHDOG" in r.getMessage() for r in caplog.records)

    def test_recycle_failure_does_not_kill_loop(self, monkeypatch, app):
        self._run_one_cycle(
            monkeypatch, app,
            sweep_result={"time_exhausted": 0, "stuck_cancelled": 0, "kicked": 0},
            recycle_exc=RuntimeError("no cluster"))
        run = wd.last_run()
        assert run is not None
        assert "pods_checked" not in run["summary"]

    def test_sweep_failure_is_swallowed(self, monkeypatch, app):
        self._run_one_cycle(monkeypatch, app, sweep_exc=RuntimeError("db down"))
        assert wd.last_run() is None  # 本轮未成功落 summary


class TestStartStop:
    def test_start_refused_when_disabled(self, monkeypatch, app):
        monkeypatch.delenv("GOAL_LOOP_WATCHDOG_ENABLED", raising=False)
        assert wd.start_goal_loop_watchdog(app) is False
        assert wd._scheduler_thread is None

    def test_start_is_idempotent_and_stop_resets(self, monkeypatch, app):
        monkeypatch.setenv("GOAL_LOOP_WATCHDOG_ENABLED", "true")
        monkeypatch.setattr(wd, "interval_seconds", lambda default=300: 30)
        assert wd.start_goal_loop_watchdog(app) is True
        assert wd.start_goal_loop_watchdog(app) is False  # 已在岗

        wd.stop_goal_loop_watchdog()
        assert wd._scheduler_thread is None

        # 停止后可再次启动（重启路径）
        assert wd.start_goal_loop_watchdog(app) is True
        wd.stop_goal_loop_watchdog()

    def test_stop_without_thread_is_safe(self):
        wd.stop_goal_loop_watchdog()
        assert wd._scheduler_thread is None

    def test_stop_before_first_wait_interrupts_promptly(self, monkeypatch, app):
        monkeypatch.setenv("GOAL_LOOP_WATCHDOG_ENABLED", "true")
        monkeypatch.setattr(wd, "interval_seconds", lambda default=300: 3600)
        wd.start_goal_loop_watchdog(app)
        wd.stop_goal_loop_watchdog()  # join(timeout=5) 内退出，不真等 1 小时
        assert wd._scheduler_thread is None
