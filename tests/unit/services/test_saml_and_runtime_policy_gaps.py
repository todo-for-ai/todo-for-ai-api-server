"""services/saml.py 与 services/workspace_runtime_policy.py 缺口清扫。

saml：元数据解析回退与缺证书报错、fetch_idp_metadata 注入客户端与
空 URL、enveloped 签名验证的各 False 分支（无签名/缺 SignedInfo/
未知摘要与签名算法/摘要不匹配/签名值损坏）、verify_saml_response
各错误分支（坏 base64/非 Response/非 Success/Response 级签名回退/
缺 Assertion/issuer 不匹配/NotBefore 未生效/audience 不匹配/
InResponseTo 不匹配/缺 email NameID）、小数秒时间解析；
runtime_policy：_list_all_agent_pods 异常静默、labels 解析失败与
无 agent_id 跳过、非 Running/Pending phase 跳过、_last_activity_at
无记录、_parse_ts 各形态。
"""

import base64
import datetime as dt
import secrets
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from services.saml import (
    NS_DS,
    NS_SAML,
    NS_SAMLP,
    SAMLError,
    _parse_saml_time,
    fetch_idp_metadata,
    parse_idp_metadata,
    verify_enveloped_signature,
    verify_saml_response,
)
from services.workspace_runtime_policy import (
    _last_activity_at,
    _parse_ts,
    recycle_idle_pods,
)

SP_ENTITY = 'https://todo4ai.example.com/sso/metadata'
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
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509 import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "test-idp"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow() - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )
    return {
        "cert_pem": cert.public_bytes(
            serialization.Encoding.PEM).decode(),
        "private_pem": private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode(),
    }


def _build_idp_metadata(cert_pem: str) -> bytes:
    der = cert_pem.replace(
        "-----BEGIN CERTIFICATE-----\n", "").replace(
        "\n-----END CERTIFICATE-----", "").replace("\n", "")
    return (
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'xmlns:ds="{NS_DS}" entityID="{IDP_ENTITY}">'
        f'<md:IDPSSODescriptor>'
        f'<md:KeyDescriptor><ds:KeyInfo><ds:X509Data>'
        f'<ds:X509Certificate>{der}</ds:X509Certificate></ds:X509Data>'
        f'</ds:KeyInfo></md:KeyDescriptor>'
        f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="https://idp.example.com/sso"/>'
        f'</md:IDPSSODescriptor></md:EntityDescriptor>'
    ).encode()


def _sign_assertion(assertion_xml: str, private_pem: str) -> str:
    """与 services.saml 验证器同一摘要/签名管线（复用既有实现逻辑）。"""
    import hashlib

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    from services.saml import _c14n, _strip_signature

    root = ET.fromstring(assertion_xml)
    reference_id = root.get("ID")
    private_key = serialization.load_pem_private_key(
        private_pem.encode(), password=None)
    digest = hashlib.sha256(_c14n(_strip_signature(root))).digest()
    signed_info_xml = (
        f'<ds:SignedInfo xmlns:ds="{NS_DS}">'
        f'<ds:CanonicalizationMethod Algorithm="http://docs.oasis-open.org/xmldsig/2006/xml-exc-c14n#"/>'
        f'<ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/>'
        f'<ds:Reference URI="#{reference_id}">'
        f'<ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>'
        f'<ds:DigestValue>{base64.b64encode(digest).decode()}</ds:DigestValue>'
        f'</ds:Reference></ds:SignedInfo>'
    )
    signed_info = ET.fromstring(signed_info_xml)
    signature_value = private_key.sign(
        _c14n(signed_info), padding.PKCS1v15(), hashes.SHA256())
    signature_element = ET.fromstring(
        f'<ds:Signature xmlns:ds="{NS_DS}">{ET.tostring(signed_info, encoding="unicode")}'
        f'<ds:SignatureValue>{base64.b64encode(signature_value).decode()}</ds:SignatureValue>'
        f'</ds:Signature>')
    root.append(signature_element)
    return ET.tostring(root, encoding="unicode")


def _build_response(idp_keys, *, audience=SP_ENTITY, issuer=IDP_ENTITY,
                    email="saml-user@example.com", request_id="req-1",
                    sign_level="assertion", not_before_offset_minutes=-1):
    """构建（可选 Response 级/Assertion 级签名）的 SAMLResponse b64。"""
    now = dt.datetime.utcnow()
    assertion_id = "_" + secrets.token_urlsafe(16)
    confirmation_data = (
        f'<saml:SubjectConfirmationData InResponseTo="{request_id}" '
        f'NotOnOrAfter="{(now + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'Recipient="https://app.example.com/sso/callback"/>')
    name_id = (f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:'
               f'nameid-format:email">{email}</saml:NameID>'
               if email else '')
    assertion = (
        f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="{assertion_id}" Version="2.0" '
        f'IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<saml:Issuer>{issuer}</saml:Issuer>'
        f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:email">{email}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'{confirmation_data}</saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{(now + dt.timedelta(minutes=not_before_offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'NotOnOrAfter="{(now + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>'
        f'</saml:Conditions></saml:Assertion>'
    )
    if name_id == '':
        assertion = assertion.replace(
            f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:'
            f'nameid-format:email">{email}</saml:NameID>', '')
    if sign_level == "assertion":
        assertion = _sign_assertion(assertion, idp_keys["private_pem"])
    response = (
        f'<samlp:Response xmlns:samlp="{NS_SAMLP}" ID="_resp{secrets.token_urlsafe(8)}" Version="2.0" '
        f'IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
        f'<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f'{assertion}'
        f'</samlp:Response>'
    )
    if sign_level == "response":
        response = _sign_assertion(response, idp_keys["private_pem"])
    return base64.b64encode(response.encode()).decode()


# ─────────────────────────── 元数据解析 ───────────────────────────


class TestIdpMetadata:
    def test_first_binding_not_redirect_falls_back(self, idp_keys):
        der = idp_keys["cert_pem"].replace(
            "-----BEGIN CERTIFICATE-----\n", "").replace(
            "\n-----END CERTIFICATE-----", "").replace("\n", "")
        xml = (
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'xmlns:ds="{NS_DS}" entityID="{IDP_ENTITY}">'
            f'<md:IDPSSODescriptor>'
            f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:SOAP" '
            f'Location="https://idp.example.com/soap"/>'
            f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            f'Location="https://idp.example.com/sso"/>'
            f'<md:KeyDescriptor><ds:KeyInfo><ds:X509Data>'
            f'<ds:X509Certificate>{der}</ds:X509Certificate></ds:X509Data>'
            f'</ds:KeyInfo></md:KeyDescriptor>'
            f'</md:IDPSSODescriptor></md:EntityDescriptor>'
        ).encode()
        parsed = parse_idp_metadata(xml)
        assert parsed["sso_url"] == "https://idp.example.com/sso"
        assert "BEGIN CERTIFICATE" in parsed["certificate_pem"]

    def test_missing_certificate_raises(self):
        xml = (
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata">'
            f'<md:IDPSSODescriptor>'
            f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            f'Location="https://idp.example.com/sso"/>'
            f'</md:IDPSSODescriptor></md:EntityDescriptor>'
        ).encode()
        with pytest.raises(SAMLError, match="missing SSO URL"):
            parse_idp_metadata(xml)

    def test_fetch_with_injected_client(self, idp_keys):
        class _Client:
            def __init__(self):
                self.closed = False

            def get(self, url):
                return SimpleNamespace(
                    content=_build_idp_metadata(idp_keys["cert_pem"]),
                    raise_for_status=lambda: None)

            def close(self):
                self.closed = True

        injected = _Client()
        parsed = fetch_idp_metadata("https://idp/meta",
                                    http_client=injected)
        assert parsed["sso_url"] == "https://idp.example.com/sso"
        assert injected.closed is False  # 注入的客户端不代管生命周期

    def test_fetch_with_owned_client_closes(self, idp_keys, monkeypatch):
        import httpx
        closed = []

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            def get(self, url):
                return SimpleNamespace(
                    content=_build_idp_metadata(idp_keys["cert_pem"]),
                    raise_for_status=lambda: None)

            def close(self):
                closed.append(True)

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        fetch_idp_metadata("https://idp/meta")
        assert closed == [True]

    def test_fetch_empty_url_raises(self):
        with pytest.raises(SAMLError, match="not configured"):
            fetch_idp_metadata("")


# ─────────────────────────── 签名验证分支 ───────────────────────────


class TestVerifyEnvelopedSignature:
    def _signed_assertion(self, idp_keys):
        return _sign_assertion(
            f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="_a1" Version="2.0">'
            f'<saml:Issuer>{IDP_ENTITY}</saml:Issuer></saml:Assertion>',
            idp_keys["private_pem"])

    def test_no_signature_returns_false(self):
        root = ET.fromstring(
            f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="_a1"/>')
        assert verify_enveloped_signature(root, "cert") is False

    def test_signature_without_signed_info_returns_false(self):
        root = ET.fromstring(
            f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="_a1" '
            f'xmlns:ds="{NS_DS}"><ds:Signature/></saml:Assertion>')
        assert verify_enveloped_signature(root, "cert") is False

    def test_unknown_digest_algorithm_returns_false(self, idp_keys):
        signed = self._signed_assertion(idp_keys)
        root = ET.fromstring(signed)
        ref = root.find(f'{{{NS_DS}}}Signature/{{{NS_DS}}}SignedInfo/'
                        f'{{{NS_DS}}}Reference/{{{NS_DS}}}DigestMethod')
        ref.set("Algorithm", "http://example.com/unknown-digest")
        assert verify_enveloped_signature(
            root, idp_keys["cert_pem"]) is False

    def test_unknown_signature_algorithm_returns_false(self, idp_keys):
        signed = self._signed_assertion(idp_keys)
        root = ET.fromstring(signed)
        method = root.find(f'{{{NS_DS}}}Signature/{{{NS_DS}}}SignedInfo/'
                           f'{{{NS_DS}}}SignatureMethod')
        method.set("Algorithm", "http://example.com/unknown-sig")
        assert verify_enveloped_signature(
            root, idp_keys["cert_pem"]) is False

    def test_digest_mismatch_returns_false(self, idp_keys):
        signed = self._signed_assertion(idp_keys)
        root = ET.fromstring(signed)
        issuer = root.find(f'{{{NS_SAML}}}Issuer')
        issuer.text = "tampered-after-signing"
        assert verify_enveloped_signature(
            root, idp_keys["cert_pem"]) is False

    def test_corrupted_signature_value_returns_false(self, idp_keys):
        signed = self._signed_assertion(idp_keys)
        root = ET.fromstring(signed)
        value = root.find(f'{{{NS_DS}}}Signature/'
                          f'{{{NS_DS}}}SignatureValue')
        value.text = base64.b64encode(b"garbage-signature").decode()
        assert verify_enveloped_signature(
            root, idp_keys["cert_pem"]) is False

    def test_reference_uri_targets_missing_element_returns_false(
            self, idp_keys):
        signed = self._signed_assertion(idp_keys)
        root = ET.fromstring(signed)
        ref = root.find(f'{{{NS_DS}}}Signature/{{{NS_DS}}}SignedInfo/'
                        f'{{{NS_DS}}}Reference')
        ref.set("URI", "#nonexistent-element")
        assert verify_enveloped_signature(
            root, idp_keys["cert_pem"]) is False


# ─────────────────────────── SAMLResponse 校验 ───────────────────────────


class TestVerifySamlResponseErrors:
    def _verify(self, idp_keys, b64, **kw):
        return verify_saml_response(
            b64, idp_entity_id=IDP_ENTITY, sp_entity_id=SP_ENTITY,
            certificate_pem=idp_keys["cert_pem"], **kw)

    def test_invalid_base64_raises(self, idp_keys):
        with pytest.raises(SAMLError, match="invalid SAMLResponse"):
            self._verify(idp_keys, "!!!not-base64!!!")

    def test_non_response_root_raises(self, idp_keys):
        b64 = base64.b64encode(
            f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="_a1"/>'
            .encode()).decode()
        with pytest.raises(SAMLError, match="not a samlp:Response"):
            self._verify(idp_keys, b64)

    def test_non_success_status_raises(self, idp_keys):
        b64 = base64.b64encode((
            f'<samlp:Response xmlns:samlp="{NS_SAMLP}" ID="_r" Version="2.0">'
            f'<samlp:Status><samlp:StatusCode '
            f'Value="urn:oasis:names:tc:SAML:2.0:status:Responder"/></samlp:Status>'
            f'</samlp:Response>').encode()).decode()
        with pytest.raises(SAMLError, match="non-success"):
            self._verify(idp_keys, b64)

    def test_missing_assertion_raises(self, idp_keys):
        # Response 级签名有效但不含 Assertion → 才能到达缺 Assertion 分支
        now = dt.datetime.utcnow()
        response = (
            f'<samlp:Response xmlns:samlp="{NS_SAMLP}" ID="_respX" Version="2.0" '
            f'IssueInstant="{now.strftime("%Y-%m-%dT%H:%M:%SZ")}">'
            f'<samlp:Status><samlp:StatusCode '
            f'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            f'</samlp:Response>')
        signed = _sign_assertion(response, idp_keys["private_pem"])
        b64 = base64.b64encode(signed.encode()).decode()
        with pytest.raises(SAMLError, match="missing saml:Assertion"):
            self._verify(idp_keys, b64)

    def test_unsigned_rejected(self, idp_keys):
        b64 = _build_response(idp_keys, sign_level=None)
        with pytest.raises(SAMLError):
            self._verify(idp_keys, b64)

    def test_response_level_signature_accepted(self, idp_keys):
        b64 = _build_response(idp_keys, sign_level="response")
        parsed = self._verify(idp_keys, b64, request_id="req-1")
        assert parsed["email"] == "saml-user@example.com"

    def test_issuer_mismatch_raises(self, idp_keys):
        b64 = _build_response(idp_keys, issuer="https://evil.example/idp")
        with pytest.raises(SAMLError, match="issuer mismatch"):
            self._verify(idp_keys, b64)

    def test_not_yet_valid_raises(self, idp_keys):
        b64 = _build_response(idp_keys, not_before_offset_minutes=10)
        with pytest.raises(SAMLError, match="not yet valid"):
            self._verify(idp_keys, b64)

    def test_audience_mismatch_raises(self, idp_keys):
        b64 = _build_response(idp_keys,
                              audience="https://other.example/sp")
        with pytest.raises(SAMLError, match="audience mismatch"):
            self._verify(idp_keys, b64)

    def test_in_response_to_mismatch_raises(self, idp_keys):
        b64 = _build_response(idp_keys, request_id="req-orig")
        with pytest.raises(SAMLError, match="InResponseTo mismatch"):
            self._verify(idp_keys, b64, request_id="req-other")

    def test_missing_email_name_id_raises(self, idp_keys):
        b64 = _build_response(idp_keys, email="")
        with pytest.raises(SAMLError, match="missing email NameID"):
            self._verify(idp_keys, b64)


    def test_assertion_expired_raises(self, idp_keys):
        # 过期检查在签名校验之后，需构造签名有效但已过期的断言
        now = dt.datetime.utcnow()
        assertion = (
            f'<saml:Assertion xmlns:saml="{NS_SAML}" ID="_expired" Version="2.0">'
            f'<saml:Issuer>{IDP_ENTITY}</saml:Issuer>'
            f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:'
            f'nameid-format:email">saml-user@example.com</saml:NameID></saml:Subject>'
            f'<saml:Conditions NotBefore="{(now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}" '
            f'NotOnOrAfter="{(now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}"/>'
            f'</saml:Assertion>')
        signed_assertion = _sign_assertion(assertion,
                                           idp_keys["private_pem"])
        response = (
            f'<samlp:Response xmlns:samlp="{NS_SAMLP}" ID="_r" Version="2.0">'
            f'<samlp:Status><samlp:StatusCode '
            f'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            f'{signed_assertion}</samlp:Response>')
        b64 = base64.b64encode(response.encode()).decode()
        with pytest.raises(SAMLError, match="assertion expired"):
            self._verify(idp_keys, b64)



class TestParseSamlTime:
    def test_fractional_seconds_with_z(self):
        parsed = _parse_saml_time("2026-09-09T12:00:00.123456Z")
        assert parsed == dt.datetime(2026, 9, 9, 12, 0, 0)

    def test_plain_utc(self):
        assert _parse_saml_time("2026-09-09T12:00:00Z") == \
            dt.datetime(2026, 9, 9, 12, 0)



class TestSamlRequestBuilders:
    def test_new_request_id_format(self):
        from services.saml import new_request_id
        rid = new_request_id()
        assert rid.startswith("_")
        assert "-" not in rid

    def test_build_authn_request_fixed_instant(self):
        from services.saml import build_authn_request
        xml = build_authn_request(
            SP_ENTITY, "https://app.example.com/acs", "_req1",
            issue_instant=dt.datetime(2026, 9, 9, 8, 30))
        assert 'IssueInstant="2026-09-09T08:30:00Z"' in xml
        assert SP_ENTITY in xml

    def test_build_saml_redirect_url(self):
        import zlib
        from urllib.parse import parse_qs, urlparse
        from services.saml import build_saml_redirect
        url = build_saml_redirect(
            "https://idp.example.com/sso", "<AuthnRequest/>", "state-1")
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        query = parse_qs(parsed.query)
        saml_request = base64.b64decode(query["SAMLRequest"][0])
        # deflate raw（去 zlib 头尾）逆变换
        import zlib as _z
        xml = _z.decompress(saml_request, -15).decode()
        assert xml == "<AuthnRequest/>"
        assert query["RelayState"] == ["state-1"]

    def test_build_saml_redirect_separator_for_existing_query(self):
        from services.saml import build_saml_redirect
        url = build_saml_redirect(
            "https://idp.example.com/sso?existing=1", "<A/>", "s")
        assert "&SAMLRequest=" in url


# ─────────────────────────── runtime_policy 缺口 ───────────────────────────


class _BoomProvider:
    name = "boom"
    MAX_PODS_PER_WORKSPACE = 10
    POD_IDLE_TIMEOUT_MINUTES = 30

    def list_runtimes(self, workspace_id=None):
        raise RuntimeError("backend unreachable")


class TestRuntimePolicyGaps:
    def test_recycle_swallows_cluster_error(self):
        result = recycle_idle_pods(_BoomProvider())
        assert result == {"checked": 0, "recycled": 0,
                          "skipped_active": 0, "skipped_recent": 0}

    def test_recycle_skips_bad_ids_and_phases(self):
        runtimes = [
            {"agent_id": None, "workspace_id": 1, "phase": "Running",
             "started_at": None},
            {"agent_id": 0, "workspace_id": 1, "phase": "Running",
             "started_at": None},
            {"agent_id": 12, "workspace_id": 1, "phase": "Failed",
             "started_at": None},
        ]
        provider = SimpleNamespace(
            name="fake",
            MAX_PODS_PER_WORKSPACE=10,
            POD_IDLE_TIMEOUT_MINUTES=30,
            list_runtimes=lambda workspace_id=None: runtimes,
            terminate=lambda aid: True)
        result = recycle_idle_pods(provider)
        assert result["checked"] == 3
        assert result["recycled"] == 0

    def test_started_at_parse_variants(self):
        naive = dt.datetime(2026, 9, 9, 8, 0)
        aware = naive.replace(tzinfo=dt.timezone.utc)
        assert _parse_ts("2026-09-09T08:00:00") == naive
        assert _parse_ts(aware) == naive
        assert _parse_ts("2026-09-09T08:00:00+00:00") == naive
        assert _parse_ts(None) is None
        assert _parse_ts("garbage") is None

        assert _parse_ts(0) is None
        assert _parse_ts(dt.datetime(2026, 9, 9, 8, 0, tzinfo=None)) == \
            dt.datetime(2026, 9, 9, 8, 0)

    def test_last_activity_at_none_without_rows(self, _isolated_app):
        assert _last_activity_at(424242) is None
