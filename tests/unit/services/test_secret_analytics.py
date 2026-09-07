"""Secret 使用分析（services/secret_analytics.py）单元回归。

覆盖：趋势按日聚合与缺失日填充、标准差异常检测（高/中等级、数据不足守卫）、
热力图分组、Top 调用者、完整报告（含 Secret 缺失分支）、工作区统计、单例。
单一内聚分析器类，不拆文件，纯补测。
"""

import uuid
from datetime import datetime, timedelta

import pytest

from models import AgentSecret, SecretAuditLog, db
from services.secret_analytics import SecretUsageAnalyzer, get_secret_analyzer


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
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
def secret():
    row = AgentSecret(
        agent_id=1, workspace_id=1, name="cfg-key",
        secret_hash="h" * 64, secret_encrypted="cipher", prefix="sk-",
        created_by_user_id=1, updated_by_user_id=1,
        usage_count=0,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _audit(secret_id, when, action="used", actor_type="user",
           actor_id=1, actor_name="alice", workspace_id=1):
    db.session.add(SecretAuditLog(
        secret_id=secret_id, workspace_id=workspace_id, action=action,
        timestamp=when, actor_type=actor_type, actor_id=actor_id,
        actor_name=actor_name,
    ))


class TestUsageTrends:
    def test_fills_missing_days_and_counts(self, secret):
        today = datetime.utcnow()
        _audit(secret.id, today.replace(hour=5, minute=0))
        _audit(secret.id, today.replace(hour=9, minute=0))
        _audit(secret.id, today - timedelta(days=2))
        _audit(secret.id, today - timedelta(days=2), action="rotated")  # 非 used 不计
        db.session.commit()

        trends = SecretUsageAnalyzer().get_usage_trends(secret.id, days=30)
        assert len(trends) == 30
        assert trends[-1]["count"] == 2
        assert trends[-3]["count"] == 1
        assert trends[-2]["count"] == 0

    def test_days_window(self, secret):
        trends = SecretUsageAnalyzer().get_usage_trends(secret.id, days=7)
        assert len(trends) == 7


class TestDetectAnomalies:
    def test_flags_peak_with_severity(self, secret, monkeypatch):
        analyzer = SecretUsageAnalyzer()
        base = [{"date": f"d{i}", "count": 1} for i in range(29)]
        peak = [{"date": "peak", "count": 50}]
        monkeypatch.setattr(analyzer, "get_usage_trends",
                            lambda sid, days=30: base + peak)
        anomalies = analyzer.detect_anomalies(secret.id, threshold=2.0)
        assert len(anomalies) == 1
        assert anomalies[0]["date"] == "peak"
        assert anomalies[0]["severity"] == "high"
        assert anomalies[0]["deviation"] > 3

    def test_medium_severity_band(self, secret, monkeypatch):
        analyzer = SecretUsageAnalyzer()
        # 均值≈10.29，σ≈0.70：count 12 落在 (mean+2σ, mean+3σ] → medium
        trends = [{"date": f"d{i}", "count": 10} for i in range(6)]
        trends.append({"date": "m-peak", "count": 12})
        monkeypatch.setattr(analyzer, "get_usage_trends",
                            lambda sid, days=30: trends)
        anomalies = analyzer.detect_anomalies(secret.id, threshold=2.0)
        assert [a["severity"] for a in anomalies] == ["medium"]

    def test_insufficient_data_guard(self, secret, monkeypatch):
        analyzer = SecretUsageAnalyzer()
        monkeypatch.setattr(analyzer, "get_usage_trends",
                            lambda sid, days=30: [{"date": "d", "count": 1}])
        assert analyzer.detect_anomalies(secret.id) == []


class TestHeatmapAndCallers:
    def test_heatmap_groups_by_weekday_hour(self, secret):
        now = datetime.utcnow()
        _audit(secret.id, now.replace(hour=3, minute=0))
        _audit(secret.id, now.replace(hour=3, minute=30))
        _audit(secret.id, now.replace(hour=15, minute=0))
        db.session.commit()

        heatmap = SecretUsageAnalyzer().get_usage_heatmap(secret.id, days=90)
        assert heatmap["total"] == 3
        assert heatmap["max_value"] == 2
        key = f"{now.weekday()}-3"
        assert heatmap["data"][key] == 2

    def test_heatmap_empty(self, secret):
        heatmap = SecretUsageAnalyzer().get_usage_heatmap(secret.id)
        assert heatmap == {"days": 90, "data": {}, "max_value": 0, "total": 0}

    def test_top_callers_ordered(self, secret):
        now = datetime.utcnow()
        for _ in range(3):
            _audit(secret.id, now, actor_id=1, actor_name="alice")
        for _ in range(1):
            _audit(secret.id, now, actor_type="agent", actor_id=2,
                   actor_name="bot")
        db.session.commit()

        callers = SecretUsageAnalyzer().get_top_callers(secret.id, limit=10)
        assert callers[0]["actor_name"] == "alice"
        assert callers[0]["count"] == 3
        assert callers[1]["actor_type"] == "agent"


class TestUsageReport:
    def test_missing_secret(self):
        assert SecretUsageAnalyzer().generate_usage_report(999999) == {
            "error": "Secret not found"}

    def test_full_report(self, secret):
        now = datetime.utcnow()
        _audit(secret.id, now)
        _audit(secret.id, now)
        db.session.commit()

        report = SecretUsageAnalyzer().generate_usage_report(secret.id, days=30)
        assert report["secret_id"] == secret.id
        assert report["secret_name"] == "cfg-key"
        assert report["summary"]["total_usage"] == 2
        assert report["summary"]["average_daily"] == 0.07
        assert report["summary"]["unique_callers"] == 1
        assert len(report["trends"]) == 30
        assert "anomalies" in report and "heatmap" in report


class TestWorkspaceStats:
    def test_counts_active_and_revoked(self, secret):
        revoked = AgentSecret(
            agent_id=1, workspace_id=1, name="old-key",
            secret_hash="h" * 64, secret_encrypted="c", prefix="sk-",
            created_by_user_id=1, updated_by_user_id=1,
            is_active=False, usage_count=7,
        )
        db.session.add(revoked)
        db.session.commit()
        _audit(secret.id, datetime.utcnow(), workspace_id=1)

        stats = SecretUsageAnalyzer().get_workspace_secret_stats(1)
        assert stats["total_secrets"] == 2
        assert stats["active_secrets"] == 1
        assert stats["revoked_secrets"] == 1
        assert stats["total_usage"] == 7
        assert stats["top_secrets"][0]["name"] == "old-key"


def test_singleton():
    assert get_secret_analyzer() is get_secret_analyzer()
    assert isinstance(get_secret_analyzer(), SecretUsageAnalyzer)
