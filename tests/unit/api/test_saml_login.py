"""Tests for SAML login flow (metadata parse, SP-initiated, signed assertion verification)."""

import base64
import datetime as dt
import sys
import os
import secrets
import zlib
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

os.environ["SECRET_ENCRYPTION_KEY"] = "uCuDTIUbpnE0Z47hrUqyNY8w7SjtIwKxnvZTduXeN30="

import pytest

BASE_URL = "/todo-for-ai/api/v1"

NS_SAMLP = 'urn:oasis:names:tc:SAML:2.0:protocol'
NS_SAML = 'urn:oasis:names:tc:SAML:2.0:assertion'
NS_DS = 'http://www.w3.org/2000/09/xmldsig#'

SP_ENTITY = 'https://todo4ai.example.com/sso/metadata'
ACS_URL = 'https://app.example.com/sso/callback'
IDP_ENTITY = 'https://idp.example.com/entity'


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    from models import db

    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
        "SECRET_KEY": "test-sso-secret",
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture(scope="session")
def idp_keys():
    """生成测试 IdP 的 RSA 密钥与自签证书。"""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "test-idp"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return {"cert_pem": cert_pem, "private_pem": private_pem.decode()}


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


@pytest.fixture
def owner_auth(_isolated_app, db_session):
    import uuid as _uuid
    from models import User, Organization
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token

    unique_id = str(_uuid.uuid4())[:8]
    user = User(username=f"testuser_{unique_id}", email=f"test_{unique_id}@example.com")
    user.password_hash = generate_password_hash("password123")
    db_session.add(user)
    db_session.commit()

    org = Organization(name=f"org-{unique_id}", slug=f"org-{unique_id}", owner_id=user.id)
    db_session.add(org)
    db_session.commit()

    with _isolated_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"user": user, "org": org, "headers": {"Authorization": f"Bearer {token}"}}


def _sign_assertion(assertion_xml: str, private_pem: str) -> str:
    """给 Assertion 插入 enveloped 签名（与 services.saml 验证器同一摘要/签名管线）。"""
    import hashlib

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from xml.etree import ElementTree as ET

    from services.saml import NS_DS, _c14n, _strip_signature

    root = ET.fromstring(assertion_xml)
    reference_id = root.get("ID")

    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)

    # 1) DigestValue：去除签名（尚无）后的 C14N 摘要
    digest = hashlib.sha256(_c14n(_strip_signature(root))).digest()

    signed_info_xml = (
        f'<ds:SignedInfo xmlns:ds="{NS_DS}">'
        f'<ds:CanonicalizationMethod Algorithm="http://docs.oasis-open.org/xmldsig/2006/xml-exc-c14n#"/>'
        f'<ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/>'
        f'<ds:Reference URI="#{reference_id}">'
        f'<ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>'
        f'<ds:DigestValue>{base64.b64encode(digest).decode()}</ds:DigestValue>'
        f'</ds:Reference>'
        f'</ds:SignedInfo>'
    )
    signed_info = ET.fromstring(signed_info_xml)

    # 2) SignatureValue：SignedInfo C14N 后 RSA-SHA256 签名
    signature_value = private_key.sign(
        _c14n(signed_info), padding.PKCS1v15(), hashes.SHA256(),
    )

    signature_element = ET.fromstring(
        f'<ds:Signature xmlns:ds="{NS_DS}">{ET.tostring(signed_info, encoding="unicode")}'
        f'<ds:SignatureValue>{base64.b64encode(signature_value).decode()}</ds:SignatureValue>'
        f'</ds:Signature>'
    )
    root.append(signature_element)
    return ET.tostring(root, encoding="unicode")


def _build_idp_metadata(cert_pem: str) -> bytes:
    der_b64 = cert_pem
    for marker in ("-----BEGIN CERTIFICATE-----\n", "\n-----END CERTIFICATE-----"):
        der_b64 = der_b64.replace(marker, "")
    der_b64 = der_b64.replace("\n", "")
    return (
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{IDP_ENTITY}">'
        f'<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        f'<md:KeyDescriptor><ds:KeyInfo xmlns:ds="{NS_DS}">'
        f'<ds:X509Data><ds:X509Certificate>{der_b64}</ds:X509Certificate></ds:X509Data>'
        f'</ds:KeyInfo></md:KeyDescriptor>'
        f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="https://idp.example.com/sso"/>'
        f'</md:IDPSSODescriptor></md:EntityDescriptor>'
    ).encode()


def _build_response(idp_keys, *, audience=SP_ENTITY, issuer=IDP_ENTITY,
                    email="saml-user@example.com", request_id=None,
                    not_on_or_after_offset_minutes=30):
    now = dt.datetime.utcnow()
    confirmation_data = (
        f'<saml:SubjectConfirmationData InResponseTo="{request_id}" '
        f'NotOnOrAfter="{(now + dt.timedelta(minutes=not_on_or_after_offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'Recipient="{ACS_URL}"/>'
        if request_id else ''
    )
    assertion_id = "_" + secrets.token_urlsafe(16)
    assertion = (
        f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="{assertion_id}" Version="2.0" '
        f'IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<saml:Issuer>{issuer}</saml:Issuer>'
        f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:email">{email}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'{confirmation_data}</saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{(now - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'NotOnOrAfter="{(now + dt.timedelta(minutes=not_on_or_after_offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>'
        f'</saml:Conditions>'
        f'<saml:AuthnStatement AuthnInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<saml:AuthnContext><saml:AuthnContextClassRef>'
        f'urn:oasis:names:tc:SAML:2.0:ac:classes:Password'
        f'</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>'
        f'</saml:Assertion>'
    )
    signed = _sign_assertion(assertion, idp_keys["private_pem"])
    response = (
        f'<samlp:Response xmlns:samlp="{NS_SAMLP}" ID="_resp{secrets.token_urlsafe(8)}" Version="2.0" '
        f'IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f'{signed}'
        f'</samlp:Response>'
    )
    return base64.b64encode(response.encode()).decode()


def _configure_saml(client, owner_auth):
    ws = owner_auth["org"].id
    resp = client.put(
        f"{BASE_URL}/workspaces/{ws}/sso/config",
        json={
            "provider": "saml", "enabled": True,
            "issuer": SP_ENTITY,            # SAML 模式下 = SP EntityID
            "idp_entity_id": IDP_ENTITY,
            "redirect_uri": ACS_URL,        # 复用为 ACS URL
        },
        headers=owner_auth["headers"],
    )
    assert resp.status_code == 200
    return ws


class TestSAMLFlow:
    def test_metadata_parse(self, idp_keys):
        from services.saml import parse_idp_metadata

        parsed = parse_idp_metadata(_build_idp_metadata(idp_keys["cert_pem"]))
        assert parsed["sso_url"] == "https://idp.example.com/sso"
        assert "BEGIN CERTIFICATE" in parsed["certificate_pem"]

    def test_login_generates_redirect_and_state(self, client, owner_auth, idp_keys):
        ws = _configure_saml(client, owner_auth)
        from services.sso import build_saml_login, make_saml_state

        state = make_saml_state(ws)
        metadata_xml = _build_idp_metadata(idp_keys["cert_pem"])

        from services import saml as saml_module
        original = saml_module.fetch_idp_metadata
        saml_module.fetch_idp_metadata = lambda url, http_client=None: saml_module.parse_idp_metadata(metadata_xml)
        try:
            result = build_saml_login(ws, state)
        finally:
            saml_module.fetch_idp_metadata = original

        assert result["redirect_url"].startswith("https://idp.example.com/sso?")
        assert "SAMLRequest=" in result["redirect_url"]

        # AuthnRequest deflate+b64 可还原且包含 SP entity
        import urllib.parse
        query = urllib.parse.parse_qs(urllib.parse.urlparse(result["redirect_url"]).query)
        xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15).decode()
        assert SP_ENTITY in xml and result["request_id"] in xml

    def test_verify_valid_assertion(self, client, db_session, owner_auth, idp_keys):
        from services.saml import verify_saml_response

        ws = _configure_saml(client, owner_auth)
        from services.sso import make_saml_state
        state = make_saml_state(ws)

        # 从 state 解出 request_id
        from services.sso import _state_serializer
        request_id = _state_serializer().loads(state)["request_id"]

        saml_response = _build_response(idp_keys, request_id=request_id)
        parsed = verify_saml_response(
            saml_response,
            idp_entity_id=IDP_ENTITY, sp_entity_id=SP_ENTITY,
            certificate_pem=idp_keys["cert_pem"], request_id=request_id,
        )
        assert parsed["email"] == "saml-user@example.com"

    def test_tampered_assertion_rejected(self, client, db_session, owner_auth, idp_keys):
        from services.saml import SAMLError, verify_saml_response
        from services.sso import make_saml_state, _state_serializer

        ws = _configure_saml(client, owner_auth)
        state = make_saml_state(ws)
        request_id = _state_serializer().loads(state)["request_id"]

        saml_response = _build_response(idp_keys, request_id=request_id)
        xml = base64.b64decode(saml_response).decode()
        # 篡改 NameID（签名不再匹配）
        tampered = xml.replace("saml-user@example.com", "attacker@evil.com")
        with pytest.raises(SAMLError):
            verify_saml_response(
                base64.b64encode(tampered.encode()).decode(),
                idp_entity_id=IDP_ENTITY, sp_entity_id=SP_ENTITY,
                certificate_pem=idp_keys["cert_pem"], request_id=request_id,
            )

    def test_expired_assertion_rejected(self, client, db_session, owner_auth, idp_keys):
        from services.saml import SAMLError, verify_saml_response
        from services.sso import make_saml_state, _state_serializer

        _configure_saml(client, owner_auth)
        state = make_saml_state(1)
        request_id = _state_serializer().loads(state)["request_id"]

        saml_response = _build_response(
            idp_keys, request_id=request_id, not_on_or_after_offset_minutes=-10,
        )
        with pytest.raises(SAMLError):
            verify_saml_response(
                saml_response,
                idp_entity_id=IDP_ENTITY, sp_entity_id=SP_ENTITY,
                certificate_pem=idp_keys["cert_pem"], request_id=request_id,
            )


class TestSAMLEndpoints:
    def test_login_endpoint_returns_redirect(self, client, owner_auth, idp_keys, monkeypatch):
        ws = _configure_saml(client, owner_auth)
        metadata_xml = _build_idp_metadata(idp_keys["cert_pem"])

        from services import saml as saml_module
        monkeypatch.setattr(saml_module, "fetch_idp_metadata",
                            lambda url, http_client=None: saml_module.parse_idp_metadata(metadata_xml))

        resp = client.post(f"{BASE_URL}/workspaces/{ws}/sso/login")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["provider"] == "saml"
        assert data["redirect_url"].startswith("https://idp.example.com/sso?")
        assert data["state"]

    def test_callback_full_chain_issues_jwt(self, client, owner_auth, idp_keys, monkeypatch):
        from models import User
        from services import saml as saml_module

        ws = _configure_saml(client, owner_auth)
        metadata_xml = _build_idp_metadata(idp_keys["cert_pem"])
        monkeypatch.setattr(saml_module, "fetch_idp_metadata",
                            lambda url, http_client=None: saml_module.parse_idp_metadata(metadata_xml))

        login = client.post(f"{BASE_URL}/workspaces/{ws}/sso/login")
        state = login.get_json()["data"]["state"]

        from services.sso import _state_serializer
        request_id = _state_serializer().loads(state)["request_id"]
        saml_response = _build_response(idp_keys, request_id=request_id)

        resp = client.post(
            f"{BASE_URL}/sso/callback/{ws}",
            data={"SAMLResponse": saml_response, "RelayState": state},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["access_token"]
        assert User.query.filter_by(email="saml-user@example.com").first() is not None

    def test_tampered_response_rejected_at_endpoint(self, client, owner_auth, idp_keys, monkeypatch):
        from services import saml as saml_module

        ws = _configure_saml(client, owner_auth)
        metadata_xml = _build_idp_metadata(idp_keys["cert_pem"])
        monkeypatch.setattr(saml_module, "fetch_idp_metadata",
                            lambda url, http_client=None: saml_module.parse_idp_metadata(metadata_xml))

        login = client.post(f"{BASE_URL}/workspaces/{ws}/sso/login")
        state = login.get_json()["data"]["state"]

        from services.sso import _state_serializer
        request_id = _state_serializer().loads(state)["request_id"]
        saml_response = _build_response(idp_keys, request_id=request_id)
        tampered = base64.b64encode(
            base64.b64decode(saml_response).decode().replace(
                "saml-user@example.com", "attacker@evil.com").encode()
        ).decode()

        resp = client.post(
            f"{BASE_URL}/sso/callback/{ws}",
            data={"SAMLResponse": tampered, "RelayState": state},
        )
        assert resp.status_code == 401
