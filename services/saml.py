"""SAML 2.0 SP 轻量实现（Phase 4 企业能力：SSO 登录链路）

基于配置字段 idp_metadata_url / idp_entity_id（与 issuer=SP EntityID、
redirect_uri=ACS URL 复用）提供：

- parse_idp_metadata：解析 IdP 元数据（SSO Redirect Binding 地址 + X509 证书）
- build_authn_request / build_saml_redirect：SP-initiated 登录（Redirect Binding，
  AuthnRequest deflate+base64）
- verify_saml_response：SAMLResponse 校验——签名验证（fail-closed：无签名拒绝）、
  IdP issuer、audience（SP EntityID）、时间窗（±90s 容差）、InResponseTo（可选），
  取 Subject NameID（email）。

签名验证为纯 Python 实现（ElementTree C14N + cryptography RSA/PKCS1v15），
覆盖 SAML 常见的 enveloped signature（Assertion 级优先，Response 级兜底）。
命名空间前缀按 SAML 惯例注册以保证序列化稳定。
"""

import base64
import copy
import datetime as _dt
import secrets
import zlib
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import structlog

logger = structlog.get_logger()

NS_SAMLP = 'urn:oasis:names:tc:SAML:2.0:protocol'
NS_SAML = 'urn:oasis:names:tc:SAML:2.0:assertion'
NS_MD = 'urn:oasis:names:tc:SAML:2.0:metadata'
NS_DS = 'http://www.w3.org/2000/09/xmldsig#'

BINDING_REDIRECT = 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect'

# 常见 SAML 命名空间前缀注册：保证 ElementTree 序列化前缀与 IdP 输出稳定一致
for _prefix, _uri in (
    ('samlp', NS_SAMLP), ('saml', NS_SAML), ('md', NS_MD), ('ds', NS_DS),
    ('xs', 'http://www.w3.org/2001/XMLSchema'),
    ('xsi', 'http://www.w3.org/2001/XMLSchema-instance'),
):
    ET.register_namespace(_prefix, _uri)

CLOCK_SKEW_SECONDS = 90

# digest/signature 算法 URI → hashlib/cryptography 名称
_DIGEST_ALGS = {
    'http://www.w3.org/2000/09/xmldsig#sha1': 'sha1',
    'http://www.w3.org/2001/04/xmlenc#sha256': 'sha256',
    'http://www.w3.org/2001/04/xmldsig-more#sha384': 'sha384',
    'http://www.w3.org/2001/04/xmlenc#sha512': 'sha512',
}
_SIG_ALGS = {
    'http://www.w3.org/2000/09/xmldsig#rsa-sha1': 'sha1',
    'http://www.w3.org/2001/04/xmldsig-more#rsa-sha256': 'sha256',
    'http://www.w3.org/2001/04/xmldsig-more#rsa-sha384': 'sha384',
    'http://www.w3.org/2001/04/xmldsig-more#rsa-sha512': 'sha512',
}


class SAMLError(ValueError):
    """SAML 协议/校验错误（fail-closed 统一入口）。"""


# ── 元数据解析 ──

def parse_idp_metadata(xml_data: bytes) -> Dict[str, Optional[str]]:
    """解析 IdP 元数据，返回 {sso_url, certificate_pem}。"""
    root = ET.fromstring(xml_data)
    sso_url = None
    for sso_service in root.iter(f'{{{NS_MD}}}SingleSignOnService'):
        if sso_service.get('Binding') == BINDING_REDIRECT:
            sso_url = sso_service.get('Location')
            break
        sso_url = sso_url or sso_service.get('Location')

    certificate_pem = None
    for cert_node in root.iter(f'{{{NS_DS}}}X509Certificate'):
        der_b64 = (cert_node.text or '').strip()
        if der_b64:
            certificate_pem = (
                '-----BEGIN CERTIFICATE-----\n'
                + '\n'.join(der_b64[i:i + 64] for i in range(0, len(der_b64), 64))
                + '\n-----END CERTIFICATE-----'
            )
            break

    if not sso_url or not certificate_pem:
        raise SAMLError('IdP metadata missing SSO URL or X509 certificate')
    return {'sso_url': sso_url, 'certificate_pem': certificate_pem}


def fetch_idp_metadata(metadata_url: str, http_client=None) -> Dict[str, Optional[str]]:
    """拉取并解析 IdP 元数据（http_client 可注入，测试免真实网络）。"""
    import httpx as _httpx

    if not metadata_url:
        raise SAMLError('idp_metadata_url not configured')
    client = http_client or _httpx.Client(timeout=10)
    try:
        resp = client.get(metadata_url)
        resp.raise_for_status()
        return parse_idp_metadata(resp.content)
    finally:
        if http_client is None:
            client.close()


# ── SP-initiated 登录 ──

def new_request_id() -> str:
    return '_' + secrets.token_urlsafe(24).replace('-', '_')


def build_authn_request(sp_entity_id: str, acs_url: str, request_id: str,
                        issue_instant: Optional[_dt.datetime] = None) -> str:
    instant = (issue_instant or _dt.datetime.utcnow()).strftime('%Y-%m-%dT%H:%M:%SZ')
    return (
        f'<samlp:AuthnRequest xmlns:samlp="{NS_SAMLP}" xmlns:saml="{NS_SAML}" '
        f'ID="{request_id}" Version="2.0" '
        f'IssueInstant="{instant}" '
        f'AssertionConsumerServiceURL="{acs_url}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f'<saml:Issuer>{sp_entity_id}</saml:Issuer>'
        f'</samlp:AuthnRequest>'
    )


def build_saml_redirect(sso_url: str, authn_request_xml: str, relay_state: str) -> str:
    """Redirect Binding：AuthnRequest → deflate + base64 → query。"""
    compressed = zlib.compress(authn_request_xml.encode())[2:-4]
    query = urlencode({
        'SAMLRequest': base64.b64encode(compressed).decode(),
        'RelayState': relay_state,
    })
    separator = '&' if '?' in sso_url else '?'
    return f'{sso_url}{separator}{query}'


# ── 响应校验 ──

def _c14n(element: ET.Element) -> bytes:
    return ET.canonicalize(xml_data=ET.tostring(element, encoding='unicode'),
                           strip_text=False).encode()


def _b64d(value: str) -> bytes:
    return base64.b64decode(value)


def _load_public_key(certificate_pem: str):
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    return cert.public_key()


def _strip_signature(element: ET.Element) -> ET.Element:
    """返回移除直接子 ds:Signature 后的元素副本（enveloped transform）。"""
    cloned = copy.deepcopy(element)
    for signature in cloned.findall(f'{{{NS_DS}}}Signature'):
        cloned.remove(signature)
    return cloned


def _find_signed_element(root: ET.Element, reference_id: str) -> Optional[ET.Element]:
    target = None
    for node in root.iter():
        if node.get('ID') == reference_id:
            target = node
            break
    return target


def verify_enveloped_signature(root: ET.Element, certificate_pem: str) -> bool:
    """验证 root 的直接子 ds:Signature（enveloped）。

    步骤：DigestValue 比对（对被签元素去除签名后 C14N 摘要）
    → SignedInfo C14N 后用 IdP 证书公钥 RSA/PKCS1v15 验签。
    """
    signature = root.find(f'{{{NS_DS}}}Signature')
    if signature is None:
        return False

    signed_info = signature.find(f'{{{NS_DS}}}SignedInfo')
    reference = signed_info.find(f'{{{NS_DS}}}Reference') if signed_info is not None else None
    if signed_info is None or reference is None:
        return False

    digest_alg = _DIGEST_ALGS.get(reference.find(f'{{{NS_DS}}}DigestMethod').get('Algorithm', ''))
    digest_value = reference.find(f'{{{NS_DS}}}DigestValue').text or ''
    reference_uri = reference.get('URI') or ''
    reference_id = reference_uri[1:] if reference_uri.startswith('#') else reference_uri

    signature_method = signed_info.find(f'{{{NS_DS}}}SignatureMethod')
    sig_alg = _SIG_ALGS.get(signature_method.get('Algorithm', '')) if signature_method is not None else None
    signature_value_node = signature.find(f'{{{NS_DS}}}SignatureValue')
    signature_value = signature_value_node.text if signature_value_node is not None else ''
    if not digest_alg or not sig_alg:
        return False

    target = _find_signed_element(root, reference_id) if reference_id else root
    if target is None:
        return False

    import hashlib

    computed_digest = hashlib.new(digest_alg, _c14n(_strip_signature(target))).digest()
    if computed_digest != _b64d(digest_value):
        logger.warning("saml.digest_mismatch", reference_id=reference_id)
        return False

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = _load_public_key(certificate_pem)
    try:
        public_key.verify(
            _b64d(signature_value),
            _c14n(signed_info),
            padding.PKCS1v15(),
            getattr(hashes, sig_alg.upper())(),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("saml.signature_verify_failed", error=str(e))
        return False


def verify_saml_response(saml_response_b64: str, *, idp_entity_id: str,
                         sp_entity_id: str, certificate_pem: str,
                         request_id: Optional[str] = None) -> Dict[str, Any]:
    """校验 SAMLResponse 并返回 {email, name, in_response_to}。

    fail-closed：无签名 / 签名无效 / issuer / audience / 时间窗任一不过即抛 SAMLError。
    """
    from datetime import datetime, timedelta

    try:
        root = ET.fromstring(base64.b64decode(saml_response_b64))
    except Exception as e:  # noqa: BLE001
        raise SAMLError(f'invalid SAMLResponse: {e}') from e

    if root.tag != f'{{{NS_SAMLP}}}Response':
        raise SAMLError('not a samlp:Response')
    status_code = root.find(
        f'{{{NS_SAMLP}}}Status/{{{NS_SAMLP}}}StatusCode'
    )
    if status_code is not None and not str(status_code.get('Value', '')).endswith(':Success'):
        raise SAMLError('IdP returned non-success status')

    assertion = root.find(f'{{{NS_SAML}}}Assertion')

    # 签名：Assertion 级优先，Response 级兜底；都无 → 拒绝
    signature_ok = False
    if assertion is not None and assertion.find(f'{{{NS_DS}}}Signature') is not None:
        signature_ok = verify_enveloped_signature(assertion, certificate_pem)
    elif root.find(f'{{{NS_DS}}}Signature') is not None:
        signature_ok = verify_enveloped_signature(root, certificate_pem)
    if not signature_ok:
        raise SAMLError('missing or invalid SAML signature')

    if assertion is None:
        raise SAMLError('missing saml:Assertion')

    # issuer（IdP entity）
    assertion_issuer = assertion.find(f'{{{NS_SAML}}}Issuer')
    if idp_entity_id and (assertion_issuer is None or
                          (assertion_issuer.text or '').strip() != idp_entity_id):
        raise SAMLError('assertion issuer mismatch')

    # 时间窗 + audience
    now = datetime.utcnow()
    conditions = assertion.find(f'{{{NS_SAML}}}Conditions')
    if conditions is not None:
        not_before = conditions.get('NotBefore')
        not_on_or_after = conditions.get('NotOnOrAfter')
        if not_before:
            if now < _parse_saml_time(not_before) - timedelta(seconds=CLOCK_SKEW_SECONDS):
                raise SAMLError('assertion not yet valid')
        if not_on_or_after:
            if now >= _parse_saml_time(not_on_or_after) + timedelta(seconds=CLOCK_SKEW_SECONDS):
                raise SAMLError('assertion expired')

        audience_ok = sp_entity_id is None
        for audience in conditions.iter(f'{{{NS_SAML}}}Audience'):
            if (audience.text or '').strip() == sp_entity_id:
                audience_ok = True
                break
        if not audience_ok:
            raise SAMLError('audience mismatch')

    # InResponseTo（若断言携带 SubjectConfirmationData）
    if request_id:
        for confirmation_data in assertion.iter(
            f'{{{NS_SAML}}}SubjectConfirmationData'
        ):
            in_response_to = confirmation_data.get('InResponseTo')
            if in_response_to and in_response_to != request_id:
                raise SAMLError('InResponseTo mismatch')

    name_id = assertion.find(
        f'{{{NS_SAML}}}Subject/{{{NS_SAML}}}NameID'
    )
    email = (name_id.text or '').strip() if name_id is not None else ''
    if not email or '@' not in email:
        raise SAMLError('assertion missing email NameID')

    return {'email': email.lower(), 'name': email.split('@')[0], 'in_response_to': request_id}


def _parse_saml_time(value: str) -> _dt.datetime:
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+0000'
    if '.' in text:  # 截掉小数秒，保留时区
        base, rest = text.split('.', 1)
        tz = ''.join(ch for ch in rest if not ch.isdigit())
        text = base + tz
    return _dt.datetime.strptime(text[:29], '%Y-%m-%dT%H:%M:%S%z').replace(tzinfo=None)
