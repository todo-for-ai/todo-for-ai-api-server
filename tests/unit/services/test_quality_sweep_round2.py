"""技能画像 / 洞察行动 / 市场安装三模块的缺口收口（迭代 18）。

- skill_profile：未知经验类型计为正向、重建/遗忘对不存在 Agent 的拒绝、
  空匹配词加成为 0
- insight_actions：知识覆盖统计跳过已禁用 Agent、导师不得自配、
  导师无空闲领域跳过、建议数达 limit 提前返回
- marketplace：create_agent 缺名/重名拒绝
"""

import uuid

import pytest

from models import (
    Agent,
    AgentExperience,
    AgentRoleTemplate,
    AgentStatus,
    Project,
    Task,
    User,
    db,
)


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
def user():
    u = User(username=f"gp_{uuid.uuid4().hex[:8]}", email=f"gp_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def agent(user):
    def _make(workspace_id=1, status=AgentStatus.ACTIVE, name=None):
        row = Agent(workspace_id=workspace_id, owner_id=user.id,
                    creator_user_id=user.id,
                    name=name or f"ga_{uuid.uuid4().hex[:6]}",
                    status=status)
        db.session.add(row)
        db.session.commit()
        return row
    return _make


class TestSkillProfileGaps:
    def test_unknown_experience_type_counts_positive(self):
        from services.skill_profile import _is_success_experience
        assert _is_success_experience("failure_pattern") is False
        assert _is_success_experience("anti_pattern") is False
        assert _is_success_experience("mystery_type") is True

    def test_rebuild_missing_agent_raises(self):
        from services.skill_profile import rebuild_skill_profile
        with pytest.raises(ValueError, match="not found"):
            rebuild_skill_profile(999999, edited_by_user_id=1)

    def test_forget_missing_agent_raises(self):
        from services.skill_profile import forget_skill_profile
        with pytest.raises(ValueError, match="not found"):
            forget_skill_profile(999999, edited_by_user_id=1)

    def test_bonus_zero_without_matched_terms(self, agent):
        from services.skill_profile import skill_profile_bonus
        assert skill_profile_bonus(agent(), []) == 0  # 无画像
        with_profile = agent()
        with_profile.skill_profile = {"skills": [{"name": "python"}]}
        db.session.commit()
        assert skill_profile_bonus(with_profile, []) == 0          # 有画像但匹配词为空
        assert skill_profile_bonus(with_profile, ["", "  "]) == 0  # 全空白同样为空

    def test_bonus_counts_matched_skills_with_cap(self, agent):
        from services.skill_profile import skill_profile_bonus
        ag = agent()
        ag.skill_profile = {"skills": [{"name": "Python"}, {"name": "Go"},
                                       {"name": "Rust"}, {"name": "SQL"},
                                       {"name": "Bash"}, {"name": "Ops"}]}
        db.session.commit()
        assert skill_profile_bonus(ag, ["python", "go"]) == 8
        ag.skill_profile = {"skills": [{"name": n} for n in
                                       ("a", "b", "c", "d", "e", "f", "g")]}
        db.session.commit()
        assert skill_profile_bonus(ag, ["a", "b", "c", "d", "e", "f", "g"]) == 20  # cap

    def test_bonus_zero_without_profile(self, agent):
        from services.skill_profile import skill_profile_bonus
        assert skill_profile_bonus(agent(), ["python"]) == 0

    def test_forget_clears_profile_and_writes_tombstone(self, agent):
        from services.skill_profile import forget_skill_profile
        ag = agent()
        ag.skill_profile = {"skills": [{"name": "python"}]}
        db.session.commit()
        result = forget_skill_profile(ag.id, edited_by_user_id=1)
        assert result["forgotten"] is True
        assert result["skills"] == []
        assert ag.skill_profile is None


class TestInsightGaps:
    def _experience(self, ag, domain, reused=0):
        db.session.add(AgentExperience(
            agent_id=ag.id, experience_type="success_pattern", domain=domain,
            times_reused=reused, is_valid=True,
        ))

    def test_coverage_skips_disabled_agents(self, agent):
        from services.insight_actions import _agent_knowledge_coverage
        from datetime import datetime, timedelta
        active = agent()
        disabled = agent(status=AgentStatus.DISABLED)
        self._experience(active, "python", reused=2)
        self._experience(disabled, "python")  # 已禁用 → 覆盖表不含它
        db.session.commit()

        coverage = _agent_knowledge_coverage(
            1, datetime.utcnow() - timedelta(days=7))
        assert active.id in coverage
        assert disabled.id not in coverage
        assert coverage[active.id]["reuses"] == 2

    def test_mentorship_recommendation_branches(self, agent, monkeypatch):
        """自配跳过 / 无空闲领域跳过 / limit 提前返回，用构造覆盖表驱动。"""
        from services import insight_actions as ia

        mentor = agent(name="mentor")
        mentee_full = agent(name="mentee-full")   # 已有导师全部领域
        mentee_a = agent(name="mentee-a")
        mentee_b = agent(name="mentee-b")

        coverage = {
            mentor.id: {"agent": mentor, "exps": 4, "reuses": 3,
                        "domains": {"python", "go"}},
            mentee_full.id: {"agent": mentee_full, "exps": 0, "reuses": 0,
                             "domains": {"python", "go"}},  # 无缺口 → 跳过
            mentee_a.id: {"agent": mentee_a, "exps": 0, "reuses": 0,
                          "domains": {"python"}},          # 缺 go → 配对 go
            mentee_b.id: {"agent": mentee_b, "exps": 0, "reuses": 0,
                          "domains": set()},               # 也可配对
        }
        monkeypatch.setattr(ia, "_agent_knowledge_coverage",
                            lambda ws, since: coverage)

        report = ia.recommend_mentorship_pairs(1, limit=5)
        assert report["has_suggestions"] is True
        assert len(report["suggestions"]) == 2
        # mentee_full 因无领域缺口被跳过（无空闲领域分支）；a→go、b→python
        assert [x["domain"] for x in report["suggestions"]] == ["go", "python"]

        # limit=2 时凑满即提前返回（mentee-full 不再被评估）
        early = ia.recommend_mentorship_pairs(1, limit=2)
        assert len(early["suggestions"]) == 2
        assert early["has_suggestions"] is True

    def test_mentor_never_paired_with_self(self, agent, monkeypatch):
        from services import insight_actions as ia
        # 构造同一 Agent 同时出现在导师池与学徒池（防御分支）
        shared = agent()
        coverage = {
            shared.id: {"agent": shared, "exps": 4, "reuses": 3,
                        "domains": {"python"}},
            -shared.id: {"agent": shared, "exps": 0, "reuses": 0,
                         "domains": set()},
        }
        monkeypatch.setattr(ia, "_agent_knowledge_coverage",
                            lambda ws, since: coverage)
        report = ia.recommend_mentorship_pairs(1, limit=5)
        assert report["suggestions"] == []
        assert report["has_suggestions"] is False


class TestMarketplaceInstall:
    def _template(self, user):
        row = AgentRoleTemplate(
            workspace_id=None, created_by_user_id=user.id,
            name=f"mkt_{uuid.uuid4().hex[:6]}", display_name="市场模板",
            category="developer", is_builtin=False,
            status="ACTIVE",
        )
        db.session.add(row)
        db.session.commit()
        return row

    def test_create_agent_requires_name(self, user):
        from services.marketplace import install_to_workspace
        template = self._template(user)
        with pytest.raises(ValueError, match="agent_name is required"):
            install_to_workspace(template, 1, user, create_agent=True,
                                 agent_name="   ")

    def test_create_agent_rejects_duplicate_name(self, user, agent):
        from services.marketplace import install_to_workspace
        template = self._template(user)
        agent(name="dup-agent")
        with pytest.raises(ValueError, match="already exists"):
            install_to_workspace(template, 1, user, create_agent=True,
                                 agent_name="dup-agent")
