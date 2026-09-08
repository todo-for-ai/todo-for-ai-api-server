"""core/google_config.py + core/notification_queue.py 单元回归。

覆盖：GoogleConfig 环境变量校验、GoogleService（init_app OAuth 注册、
get_user_info 成功/异常、create_or_update_user 四分支——按 google_id
命中/按邮箱绑定/全新用户走默认脚手架/缺邮箱与异常 None、generate_tokens、
四类新用户默认脚手架的幂等与语言检测）；通知投递 Redis 队列（入队/
批量入队过滤非数字/重试调度/到期晋升含 limit、阻塞弹出含非整数字段、
分布式锁 fail-open 与属主校验）——全部走进程内假 Redis，不触真实
Redis。
"""

import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import google_config as gc
from core import notification_queue as nq
from models import (
    ApiToken,
    ContextRule,
    CustomPrompt,
    User,
    UserSettings,
    db,
)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
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
def client(_isolated_app):
    return _isolated_app.test_client()


# ─────────────────────────── 通知队列（假 Redis）───────────────────────────


class _FakePipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def zrem(self, key, member):
        self._ops.append(("zrem", key, member))
        return self

    def lpush(self, key, *values):
        self._ops.append(("lpush", key, values))
        return self

    def execute(self):
        for op, key, arg in self._ops:
            if op == "zrem":
                self._store["zsets"].setdefault(key, {}).pop(arg, None)
            else:
                lst = self._store["lists"].setdefault(key, [])
                for v in arg:
                    lst.insert(0, v)
        return True


class _FakeRedis:
    """进程内最小 Redis：list / zset / kv / pipeline。"""

    def __init__(self):
        self.store = {"lists": {}, "zsets": {}, "kv": {}}

    def lpush(self, key, *values):
        lst = self.store["lists"].setdefault(key, [])
        for v in values:
            lst.insert(0, v)

    def zadd(self, key, mapping):
        self.store["zsets"].setdefault(key, {}).update(mapping)

    def zrangebyscore(self, key, lo, hi, start=0, num=None):
        # 与真实 Redis 一致：仅返回 member
        floor = float("-inf") if lo == "-inf" else float(lo)
        items = sorted(
            (score, member)
            for member, score in self.store["zsets"].get(key, {}).items()
            if floor <= score <= hi
        )
        members = [member for _, member in items]
        members = members[start:]
        return members[:num] if num else members

    def zrem(self, key, member):
        self.store["zsets"].setdefault(key, {}).pop(member, None)

    def brpop(self, key, timeout=None):
        lst = self.store["lists"].get(key)
        if not lst:
            return None
        return (key, lst.pop())

    def set(self, key, value, nx=False, ex=None):
        kv = self.store["kv"]
        if nx and key in kv:
            return False
        kv[key] = value
        return True

    def get(self, key):
        return self.store["kv"].get(key)

    def delete(self, key):
        self.store["kv"].pop(key, None)

    def pipeline(self):
        return _FakePipeline(self.store)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(nq, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(nq, "get_redis_client", lambda: None)


class TestNotificationQueue:
    def test_enqueue_delivery(self, fake_redis):
        assert nq.enqueue_delivery(42) is True
        assert fake_redis.store["lists"][nq.READY_QUEUE_KEY] == ["42"]

    def test_enqueue_delivery_no_redis(self, no_redis):
        assert nq.enqueue_delivery(42) is False

    def test_enqueue_deliveries_filters_non_digit(self, fake_redis):
        assert nq.enqueue_deliveries([1, "2", "abc", None, 3]) is True
        # lpush 逐个头插：['3','2','1'] 逆序
        assert fake_redis.store["lists"][nq.READY_QUEUE_KEY] == \
            ["3", "2", "1"]

    def test_enqueue_deliveries_all_filtered_still_true(self, fake_redis):
        assert nq.enqueue_deliveries(["abc"]) is True
        assert fake_redis.store["lists"] == {}

    def test_enqueue_deliveries_no_redis(self, no_redis):
        assert nq.enqueue_deliveries([1, 2]) is False

    def test_schedule_retry_with_datetime(self, fake_redis):
        run_at = datetime(2026, 9, 9, 12, 0)
        assert nq.schedule_delivery_retry(7, run_at) is True
        assert fake_redis.store["zsets"][nq.RETRY_ZSET_KEY] == \
            {"7": run_at.timestamp()}

    def test_schedule_retry_with_number(self, fake_redis):
        assert nq.schedule_delivery_retry(8, 123.5) is True
        assert fake_redis.store["zsets"][nq.RETRY_ZSET_KEY] == {"8": 123.5}

    def test_schedule_retry_no_redis(self, no_redis):
        assert nq.schedule_delivery_retry(8, 123.5) is False

    def test_promote_due_retries(self, fake_redis):
        now = datetime.utcnow()
        nq.schedule_delivery_retry(1, now - timedelta(seconds=10))
        nq.schedule_delivery_retry(2, now + timedelta(seconds=60))
        promoted = nq.promote_due_retries(now=now)
        assert promoted == 1
        assert fake_redis.store["lists"][nq.READY_QUEUE_KEY] == ["1"]
        assert fake_redis.store["zsets"][nq.RETRY_ZSET_KEY].keys() == {"2"}

    def test_promote_empty_returns_zero(self, fake_redis):
        assert nq.promote_due_retries() == 0

    def test_promote_respects_limit(self, fake_redis):
        now = datetime.utcnow()
        nq.schedule_delivery_retry(1, now)
        nq.schedule_delivery_retry(2, now)
        nq.schedule_delivery_retry(3, now)
        assert nq.promote_due_retries(now=now, limit=2) == 2

    def test_promote_no_redis(self, no_redis):
        assert nq.promote_due_retries() == 0

    def test_pop_delivery(self, fake_redis):
        assert nq.pop_delivery() is None
        nq.enqueue_delivery(9)
        assert nq.pop_delivery() == 9

    def test_pop_non_integer_value_returns_none(self, fake_redis):
        fake_redis.store["lists"][nq.READY_QUEUE_KEY] = ["not-a-number"]
        assert nq.pop_delivery() is None

    def test_pop_no_redis(self, no_redis):
        assert nq.pop_delivery() is None

    def test_acquire_lock_success_and_conflict(self, fake_redis):
        assert nq.acquire_delivery_lock(5, "worker-a") is True
        assert nq.acquire_delivery_lock(5, "worker-b") is False
        # 属主重入：nx 语义下同属主也会失败（按实现约定钉住）
        assert nq.acquire_delivery_lock(5, "worker-a") is False

    def test_acquire_lock_no_redis_fails_open(self, no_redis):
        assert nq.acquire_delivery_lock(5, "w") is True

    def test_release_lock_owner_check(self, fake_redis):
        nq.acquire_delivery_lock(6, "worker-a")
        nq.release_delivery_lock(6, "worker-b")  # 非属主：不删
        assert fake_redis.store["kv"][nq.DELIVERY_LOCK_PREFIX + "6"] == \
            "worker-a"
        nq.release_delivery_lock(6, "worker-a")
        assert nq.DELIVERY_LOCK_PREFIX + "6" not in fake_redis.store["kv"]

    def test_release_lock_no_redis(self, no_redis):
        assert nq.release_delivery_lock(6, "w") is True


# ─────────────────────────── Google 配置与服务 ───────────────────────────


GOOGLE_INFO = {
    "id": "g-123",
    "email": "guser@example.com",
    "name": "G User",
    "picture": "https://pic.example/g.png",
    "verified_email": True,
}


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid-test")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-test")


class TestGoogleConfig:
    def test_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(ValueError, match="配置缺失"):
            gc.GoogleConfig()

    def test_env_present(self, google_env):
        cfg = gc.GoogleConfig()
        assert cfg.client_id == "cid-test"
        assert cfg.client_secret == "secret-test"

    def test_init_app_registers_oauth(self, google_env, _isolated_app):
        # 构造时直接传 app：覆盖 __init__ 的 init_app 分支
        service = gc.GoogleService(app=_isolated_app)
        assert service.config.client_id == "cid-test"
        assert hasattr(service.oauth, "google")


class TestGetUserInfo:
    def test_success(self, google_env, client, monkeypatch):
        import requests as requests_mod
        payload = {"id": "g-1", "email": "a@b.c"}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(requests_mod, "get",
                            lambda *a, **kw: _Resp())
        with client.application.test_request_context():
            assert gc.GoogleService().get_user_info("tok") == payload

    def test_failure_returns_none(self, google_env, client, monkeypatch):
        import requests as requests_mod

        def boom(*a, **kw):
            raise RuntimeError("network down")
        monkeypatch.setattr(requests_mod, "get", boom)
        with client.application.test_request_context():
            assert gc.GoogleService().get_user_info("tok") is None


class TestCreateOrUpdateUser:
    def _service(self):
        return gc.GoogleService()

    def test_missing_email_returns_none(self, google_env, client):
        with client.application.test_request_context():
            assert self._service().create_or_update_user(
                {"id": "g-1"}) is None

    def test_existing_user_by_google_id_updates(self, google_env, client):
        user = User(username="g_old", email="guser@example.com",
                    google_id="g-123")
        db.session.add(user)
        db.session.commit()
        info = dict(GOOGLE_INFO, name="Renamed")
        result = self._service().create_or_update_user(info)
        assert result.id == user.id
        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert refreshed.full_name == "Renamed"

    def test_existing_user_by_email_binds_google(self, google_env, client):
        user = User(username="native", email="guser@example.com")
        db.session.add(user)
        db.session.commit()
        result = self._service().create_or_update_user(GOOGLE_INFO)
        assert result.id == user.id
        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        assert refreshed.google_id == "g-123"
        assert refreshed.provider == "google"

    def test_new_user_gets_default_scaffolding(self, google_env, client,
                                               monkeypatch):
        prompts_called = []
        monkeypatch.setattr(
            CustomPrompt, "initialize_user_defaults",
            classmethod(lambda cls, uid, language="zh-CN":
                        prompts_called.append((uid, language))))
        with client.application.test_request_context(
                headers={"Accept-Language": "zh-CN,en"}):
            user = self._service().create_or_update_user(GOOGLE_INFO)
        assert user is not None
        assert user.google_id == "g-123"
        # 默认 API Token
        assert ApiToken.query.filter_by(user_id=user.id).count() == 1
        # 默认用户设置，语言来自 Accept-Language
        settings = UserSettings.query.filter_by(user_id=user.id).first()
        assert settings.language == "zh-CN"
        # 默认全局规则
        rule = ContextRule.query.filter_by(
            user_id=user.id, project_id=None).first()
        assert rule is not None and rule.is_active is True
        # 默认提示词以检测到的语言初始化
        assert prompts_called == [(user.id, "zh-CN")]

    def test_existing_user_skips_scaffolding(self, google_env, client,
                                             monkeypatch):
        prompts_called = []
        monkeypatch.setattr(
            CustomPrompt, "initialize_user_defaults",
            classmethod(lambda cls, uid, language="zh-CN":
                        prompts_called.append(uid)))
        user = User(username="g_old", email="guser@example.com",
                    google_id="g-123")
        db.session.add(user)
        db.session.commit()
        self._service().create_or_update_user(GOOGLE_INFO)
        assert ApiToken.query.filter_by(user_id=user.id).count() == 0
        assert UserSettings.query.filter_by(user_id=user.id).first() is None
        assert prompts_called == []

    def test_scaffold_idempotent_when_rerun(self, google_env, client):
        user = User(username="g_old", email="guser@example.com",
                    google_id="g-123")
        db.session.add(user)
        db.session.commit()
        service = self._service()
        # 已有 API Token / 设置 / 全局规则时不再重复创建
        service._create_default_api_token(user)
        service._create_default_api_token(user)
        assert ApiToken.query.filter_by(user_id=user.id).count() == 1

    def test_exception_returns_none_and_rolls_back(self, google_env,
                                                   client, monkeypatch):
        class _BoomQuery:
            def filter_by(self, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(User, "query", _BoomQuery())
        with client.application.test_request_context():
            assert self._service().create_or_update_user(
                GOOGLE_INFO) is None


class TestGenerateTokens:
    def test_returns_token_pair(self, google_env, client):
        user = User(username="tk", email="tk@x.io", google_id="g-9")
        db.session.add(user)
        db.session.commit()
        with client.application.test_request_context():
            tokens = gc.GoogleService().generate_tokens(user)
        assert tokens["token_type"] == "Bearer"
        assert tokens["access_token"] and tokens["refresh_token"]

    def test_failure_returns_none(self, google_env, client, monkeypatch):
        monkeypatch.setattr(gc, "create_access_token",
                            lambda *a, **kw: 1 / 0)
        user = User(username="tk2", email="tk2@x.io")
        db.session.add(user)
        db.session.commit()
        with client.application.test_request_context():
            assert gc.GoogleService().generate_tokens(user) is None


class TestDefaultScaffoldingHelpers:
    def test_detect_user_language_matrix(self, google_env):
        service = gc.GoogleService()
        zh_user = User(username="z", email="z@x.io", locale="zh-CN")
        en_user = User(username="e", email="e@x.io", locale="en-US")
        assert service._detect_user_language(zh_user) == "zh-CN"
        assert service._detect_user_language(en_user) == "en"

        class _Req:
            headers = {"Accept-Language": "zh-TW,zh;q=0.9"}

        assert service._detect_user_language(en_user, _Req()) == "zh-CN"

    def test_default_user_settings_existing_returns_language(
            self, google_env, client):
        user = _uh("us")
        db.session.add(UserSettings(user_id=user.id, language="en",
                                    settings_data={}))
        db.session.commit()
        service = gc.GoogleService()
        assert service._create_default_user_settings(user) == "en"
        assert UserSettings.query.filter_by(user_id=user.id).count() == 1

    def test_default_user_settings_rollback_returns_default(
            self, google_env, client, monkeypatch):
        user = _uh("us2")
        db.session.commit()
        monkeypatch.setattr(UserSettings, "query", property(
            lambda self: (_ for _ in ()).throw(RuntimeError("down"))))
        service = gc.GoogleService()
        assert service._create_default_user_settings(user) == "zh-CN"

    def test_default_global_rule_existing_skips(self, google_env, client):
        user = _uh("gr")
        db.session.add(ContextRule(
            user_id=user.id, project_id=None, name="existing",
            content="c"))
        db.session.commit()
        gc.GoogleService()._create_default_global_rule(user)
        assert ContextRule.query.filter_by(user_id=user.id).count() == 1

    def test_default_global_rule_creates(self, google_env, client):
        user = _uh("gr2")
        db.session.commit()
        gc.GoogleService()._create_default_global_rule(user)
        rule = ContextRule.query.filter_by(
            user_id=user.id, project_id=None).first()
        assert rule is not None
        assert "亲密性" in rule.content

    def test_default_custom_prompts_skip_when_present(
            self, google_env, client, monkeypatch):
        user = _uh("cp")
        db.session.commit()
        called = []
        monkeypatch.setattr(
            CustomPrompt, "initialize_user_defaults",
            classmethod(lambda cls, uid, language="zh-CN":
                        called.append(uid)))
        # 用户已有提示词：直接返回
        monkeypatch.setattr(
            CustomPrompt, "query",
            SimpleNamespace(filter=lambda *a, **kw: SimpleNamespace(
                count=lambda: 1)))
        gc.GoogleService()._create_default_custom_prompts(user, "en")
        assert called == []

    def test_default_custom_prompts_initializes(
            self, google_env, client, monkeypatch):
        user = _uh("cp2")
        db.session.commit()
        called = []
        monkeypatch.setattr(
            CustomPrompt, "initialize_user_defaults",
            classmethod(lambda cls, uid, language="zh-CN":
                        called.append((uid, language))))
        gc.GoogleService()._create_default_custom_prompts(user, "en")
        assert called == [(user.id, "en")]

    def test_default_api_token_existing_skips(self, google_env, client):
        user = _uh("apitok")
        token, _ = ApiToken.generate_token(name="already")
        token.user_id = user.id
        db.session.add(token)
        db.session.commit()
        gc.GoogleService()._create_default_api_token(user)
        assert ApiToken.query.filter_by(user_id=user.id).count() == 1


    def test_default_api_token_failure_rolls_back(self, google_env, client,
                                                  monkeypatch):
        user = _uh("tokfail")
        db.session.commit()

        class _BoomQuery:
            def filter_by(self, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(ApiToken, "query", _BoomQuery())
        gc.GoogleService()._create_default_api_token(user)  # 异常被吞
        monkeypatch.undo()  # 撤销后再断言
        assert ApiToken.query.filter_by(user_id=user.id).count() == 0

    def test_default_global_rule_failure_rolls_back(self, google_env,
                                                    client, monkeypatch):
        user = _uh("rulefail")
        db.session.commit()

        class _BoomQuery:
            def filter_by(self, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(ContextRule, "query", _BoomQuery())
        gc.GoogleService()._create_default_global_rule(user)
        monkeypatch.undo()
        assert ContextRule.query.filter_by(user_id=user.id).count() == 0

    def test_default_custom_prompts_failure_rolls_back(
            self, google_env, client, monkeypatch):
        user = _uh("cpfail")
        db.session.commit()

        def boom(cls, uid, language="zh-CN"):
            raise RuntimeError("down")

        monkeypatch.setattr(CustomPrompt, "initialize_user_defaults",
                            classmethod(boom))
        gc.GoogleService()._create_default_custom_prompts(user, "en")



def _uh(prefix="gc"):
    u = User(username=f"{prefix}_{uuid4hex()}", email=f"{prefix}_{uuid4hex()}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def uuid4hex():
    import uuid
    return uuid.uuid4().hex[:8]
