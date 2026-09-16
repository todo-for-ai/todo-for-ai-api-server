"""IM 集成 + 通用接入 + Webhook 订阅中心 的 API 级测试。

覆盖：
- 飞书：url_verification 握手、token 验签 fail-closed、消息→建任务（群路由+幂等）、
  卡片回推走可覆盖 api_base（mock 上游）；
- 企业微信：GET echostr 验证（AES/SHA1 官方协议）、POST 消息→建任务；
- 通用 ingest：token 校验、字段映射、幂等 external_key；
- Webhook 订阅：CRUD、事件校验、ping 签名投递（本地 HTTP 接收器）、派发记录。

风格与 test_connectors.py 保持一致（function 级隔离 app + 内存 SQLite）。
"""

import hashlib
import hmac as hmac_mod
import json
import os
import sys
import threading
import uuid as _uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

import pytest

from services.github_app import encrypt_str

BASE_URL = "/todo-for-ai/api/v1"
LARK_SECRET = "lark-verification-token"
WECOM_TOKEN = "wecom-callback-token"
WECOM_AES_KEY = "61f466fa1e7c4d6a9f4dc9a5f4b0e6d27a8f3b1c9d2e4f5a6b7c8d9e0f1a2b3c"[:43]
GENERIC_TOKEN = "generic-ingest-token"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    os.environ["SECRET_ENCRYPTION_KEY"] = "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30="
    from app import create_app
    from models import db

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


@pytest.fixture
def ws_env(_isolated_app, client):
    """工作区 + 项目 + 三类连接器配置，返回常用句柄。"""
    import uuid as _uuid
    from models import User, Organization, Project, ExternalConnectorConfig, db
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique = str(_uuid.uuid4())[:8]
    user = User(username=f"imx_{unique}", email=f"imx_{unique}@example.com")
    user.password_hash = generate_password_hash("password123")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"org-{unique}", slug=f"org-{unique}", owner_id=user.id)
    db.session.add(org)
    db.session.flush()
    project = Project(name=f"proj-{unique}", owner_id=user.id, organization_id=org.id)
    db.session.add(project)
    db.session.flush()

    lark = ExternalConnectorConfig(
        workspace_id=org.id, provider='lark', enabled=True,
        default_project_id=project.id,
        secret_encrypted=encrypt_str(json.dumps({
            'verification_token': LARK_SECRET,
            'app_id': 'cli_test', 'app_secret': 'sh_test',
        })),
        config_json={'api_base': 'http://127.0.0.1:1', 'chats': {}},
    )
    wecom = ExternalConnectorConfig(
        workspace_id=org.id, provider='wecom', enabled=True,
        default_project_id=project.id,
        secret_encrypted=encrypt_str(json.dumps({
            'token': WECOM_TOKEN, 'encoding_aes_key': WECOM_AES_KEY,
            'corp_secret': 'corp-secret',
        })),
        config_json={'api_base': 'http://127.0.0.1:1', 'corp_id': 'corp1', 'agent_id': 1000002},
    )
    generic = ExternalConnectorConfig(
        workspace_id=org.id, provider='generic', enabled=True,
        default_project_id=project.id,
        secret_encrypted=encrypt_str(GENERIC_TOKEN),
        config_json={'mapping': {'title': 'issue.subject', 'content': 'issue.body'}},
    )
    db.session.add_all([lark, wecom, generic])
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return {
        'app': _isolated_app, 'user': user, 'org': org, 'project': project,
        'auth': {"Authorization": f"Bearer {token}"},
        'lark': lark, 'wecom': wecom, 'generic': generic,
    }


# ---------------------------------------------------------------------------
# 飞书
# ---------------------------------------------------------------------------

class TestLark:
    def test_url_verification_echoes_challenge(self, client, ws_env):
        resp = client.post(f"{BASE_URL}/connectors/lark/{ws_env['org'].id}/ingest",
                           json={'type': 'url_verification', 'challenge': 'abc123', 'token': LARK_SECRET})
        assert resp.status_code == 200
        assert resp.get_json()['data']['challenge'] == 'abc123'

    def test_rejects_bad_token(self, client, ws_env):
        resp = client.post(f"{BASE_URL}/connectors/lark/{ws_env['org'].id}/ingest",
                           json={'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1', 'token': 'wrong'},
                                 'event': {'message': {}}})
        assert resp.status_code == 401

    def test_message_creates_task_idempotent(self, client, ws_env, monkeypatch):
        from models import Task, db
        chat_id = 'oc_chat1'
        ws_env['lark'].config_json = {'api_base': 'http://127.0.0.1:1', 'chats': {chat_id: ws_env['project'].id}}
        db.session.commit()

        calls = []

        def fake_card(config, chat, task, created):
            calls.append((chat, task.id, created))
            return {}

        import services.connectors.lark as lark_mod
        monkeypatch.setattr(lark_mod, 'send_lark_task_card', fake_card)

        payload = {
            'schema': '2.0',
            'header': {'event_type': 'im.message.receive_v1', 'token': LARK_SECRET},
            'event': {'message': {'chat_id': chat_id, 'message_id': 'om_1',
                                  'message_type': 'text',
                                  'content': json.dumps({'text': '@_user_1 把登录页改蓝色 带上 logo'})}},
        }
        resp = client.post(f"{BASE_URL}/connectors/lark/{ws_env['org'].id}/ingest", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['created'] is True
        task_id = data['task_id']
        task = db.session.get(Task, task_id)
        assert task.is_ai_task is True
        assert '登录页' in task.title
        assert '@_user_1' not in task.content
        assert calls == [(chat_id, task_id, True)]

        # 同 message_id 重放 → 幂等命中
        resp2 = client.post(f"{BASE_URL}/connectors/lark/{ws_env['org'].id}/ingest", json=payload)
        assert resp2.get_json()['data']['created'] is False

    def test_non_text_event_ignored(self, client, ws_env):
        resp = client.post(f"{BASE_URL}/connectors/lark/{ws_env['org'].id}/ingest",
                           json={'schema': '2.0',
                                 'header': {'event_type': 'im.message.receive_v1', 'token': LARK_SECRET},
                                 'event': {'message': {'chat_id': 'oc_x', 'message_id': 'om_2',
                                                       'message_type': 'audio', 'content': '{}'}}})
        assert resp.status_code == 200
        assert resp.get_json()['data']['action'] == 'ignored'


# ---------------------------------------------------------------------------
# 企业微信
# ---------------------------------------------------------------------------

class TestWeCom:
    def test_get_echo_verification(self, client, ws_env):
        from services import wecom_crypto
        echo_plain = 'echo-plaintext-1234'
        encrypt = wecom_crypto.encrypt_message(WECOM_AES_KEY, echo_plain, str(ws_env['org'].id))
        ts, nonce = '1700000000', 'nonce1'
        sig = wecom_crypto.signature(WECOM_TOKEN, ts, nonce, encrypt)
        resp = client.get(
            f"{BASE_URL}/connectors/wecom/{ws_env['org'].id}/callback"
            f"?msg_signature={sig}&timestamp={ts}&nonce={nonce}&echostr={encrypt}")
        assert resp.status_code == 200
        assert resp.get_data(as_text=True) == echo_plain

    def test_get_echo_rejects_bad_signature(self, client, ws_env):
        resp = client.get(
            f"{BASE_URL}/connectors/wecom/{ws_env['org'].id}/callback"
            f"?msg_signature=bad&timestamp=1&nonce=n&echostr=xx")
        assert resp.status_code == 401

    def test_post_message_creates_task(self, client, ws_env, monkeypatch):
        from services import wecom_crypto
        inner = ('<xml><ToUserName><![CDATA[corp1]]></ToUserName>'
                 '<FromUserName><![CDATA[userA]]></FromUserName>'
                 '<CreateTime>1700000000</CreateTime>'
                 '<MsgType><![CDATA[text]]></MsgType>'
                 '<Content><![CDATA[巡检一下生产库]]></Content>'
                 '<MsgId>700001</MsgId></xml>')
        encrypt = wecom_crypto.encrypt_message(WECOM_AES_KEY, inner, str(ws_env['org'].id))
        ts, nonce = '1700000001', 'nonce2'
        sig = wecom_crypto.signature(WECOM_TOKEN, ts, nonce, encrypt)

        import services.connectors.wecom as wecom_mod
        monkeypatch.setattr(wecom_mod, 'send_wecom_task_notify',
                            lambda config, to_user, task, created: {})

        resp = client.post(
            f"{BASE_URL}/connectors/wecom/{ws_env['org'].id}/callback"
            f"?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
            data=wecom_crypto.build_encrypted_xml(encrypt, sig, ts, nonce),
            content_type='text/xml')
        assert resp.status_code == 200
        data = resp.get_json()['data']
        assert data['created'] is True


# ---------------------------------------------------------------------------
# 通用 ingest
# ---------------------------------------------------------------------------

class TestGenericIngest:
    def test_mapping_and_idempotency(self, client, ws_env):
        from models import Task, db
        body = {'issue': {'subject': '数据库慢查询告警', 'body': '表 t1 全表扫描'},
                'priority': 'high'}
        headers = {'X-Todo4AI-Token': GENERIC_TOKEN, 'X-Request-ID': 'req-1'}
        resp = client.post(f"{BASE_URL}/connectors/generic/{ws_env['org'].id}/ingest",
                           json=body, headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()['data']['created'] is True
        task_id = resp.get_json()['data']['task_id']
        task = db.session.get(Task, task_id)
        assert task.title == '数据库慢查询告警'
        assert 't1' in task.content

        resp2 = client.post(f"{BASE_URL}/connectors/generic/{ws_env['org'].id}/ingest",
                            json=body, headers=headers)
        assert resp2.get_json()['data']['created'] is False

    def test_rejects_bad_token(self, client, ws_env):
        resp = client.post(f"{BASE_URL}/connectors/generic/{ws_env['org'].id}/ingest",
                           json={'title': 'x'}, headers={'X-Todo4AI-Token': 'nope'})
        assert resp.status_code == 401

    def test_missing_title_400(self, client, ws_env):
        resp = client.post(f"{BASE_URL}/connectors/generic/{ws_env['org'].id}/ingest",
                           json={'nada': 1}, headers={'X-Todo4AI-Token': GENERIC_TOKEN})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Webhook 订阅中心
# ---------------------------------------------------------------------------

class TestWebhookSubscriptionAPI:
    def test_crud_flow(self, client, ws_env):
        ws = ws_env['org'].id
        # create
        resp = client.post(f"{BASE_URL}/workspaces/{ws}/webhooks",
                           headers=ws_env['auth'],
                           json={'url': 'https://example.com/hook', 'events': ['task.created'],
                                 'description': '对接内部OA'})
        assert resp.status_code == 200
        data = resp.get_json()['data']
        sub_id = data['subscription']['id']
        secret = data['secret']
        assert secret
        assert 'secret_encrypted' not in data['subscription']

        # 事件类型校验
        resp = client.post(f"{BASE_URL}/workspaces/{ws}/webhooks", headers=ws_env['auth'],
                           json={'url': 'https://example.com/h', 'events': ['bogus.event']})
        assert resp.status_code == 400

        # update
        resp = client.put(f"{BASE_URL}/workspaces/{ws}/webhooks/{sub_id}", headers=ws_env['auth'],
                          json={'events': ['*'], 'active': True})
        assert resp.status_code == 200

        # list
        resp = client.get(f"{BASE_URL}/workspaces/{ws}/webhooks", headers=ws_env['auth'])
        items = resp.get_json()['data']['items']
        assert len(items) == 1 and items[0]['events'] == ['*']

        # delete
        resp = client.delete(f"{BASE_URL}/workspaces/{ws}/webhooks/{sub_id}", headers=ws_env['auth'])
        assert resp.status_code == 200
        resp = client.get(f"{BASE_URL}/workspaces/{ws}/webhooks", headers=ws_env['auth'])
        assert resp.get_json()['data']['items'] == []

    def test_requires_workspace_manage_access(self, client, ws_env):
        # 无 token → 401
        resp = client.get(f"{BASE_URL}/workspaces/{ws_env['org'].id}/webhooks")
        assert resp.status_code == 401

    def test_ping_delivers_signed_event(self, client, ws_env):
        """本地 HTTP 接收器收到签名事件，验签通过，deliveries 落库。"""
        received = []

        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                received.append({'headers': dict(self.headers), 'body': body})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Hook)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            ws = ws_env['org'].id
            resp = client.post(f"{BASE_URL}/workspaces/{ws}/webhooks", headers=ws_env['auth'],
                               json={'url': f'http://127.0.0.1:{port}/hook', 'events': ['*']})
            sub_id = resp.get_json()['data']['subscription']['id']
            secret = resp.get_json()['data']['secret']

            resp = client.post(f"{BASE_URL}/workspaces/{ws}/webhooks/{sub_id}/ping",
                               headers=ws_env['auth'])
            assert resp.status_code == 200
            assert resp.get_json()['data']['delivery']['ok'] is True

            assert len(received) == 1
            sig = received[0]['headers'].get('X-Todo4AI-Signature', '')
            body = received[0]['body']
            t_part, v_part = sig.split(',', 1)
            ts = t_part.split('=')[1]
            expected = hmac_mod.new(secret.encode(), f"{ts}.".encode() + body,
                                    hashlib.sha256).hexdigest()
            assert v_part.split('=')[1] == expected
            assert received[0]['headers'].get('X-Todo4AI-Event') == 'task.created'

            # 派发记录
            resp = client.get(f"{BASE_URL}/workspaces/{ws}/webhooks/{sub_id}/deliveries",
                              headers=ws_env['auth'])
            assert resp.get_json()['data']['items'][0]['ok'] is True
        finally:
            server.shutdown()

    def test_dispatch_event_filters_by_type(self, ws_env):
        from models import WebhookSubscription, db
        from services.webhook_dispatcher import dispatch_event
        ws = ws_env['org'].id
        sub = WebhookSubscription(workspace_id=ws, url='http://127.0.0.1:1/x',
                                  events=['task.completed'], active=True)
        db.session.add(sub)
        db.session.commit()
        # task.created 不匹配
        assert dispatch_event(ws, 'task.created', {}, synchronous=False) == 0
        # task.completed 匹配 1 条（异步派发内部失败也不抛）
        assert dispatch_event(ws, 'task.completed', {}, synchronous=False) == 1
