"""企业微信回调加解密（官方加解密协议的完整实现）。

协议（WeCom Callback Crypto）：
- EncodingAESKey = Base64URLDecode(secret + '=')，AES-256-CBC，PKCS#7 去填充；
- 明文结构：random(16B) + msg_len(4B, 网络序) + msg + receive_id；
- 签名：sha1(字典序拼接(token, timestamp, nonce, encrypt))；
- 加密为逆过程（回复被动消息时使用）。

仅依赖 hashlib/base64 + cryptography（api-server venv 已有）。
"""

import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(ValueError):
    """加解密/验签失败。"""


def _aes_key(encoding_aes_key: str) -> bytes:
    try:
        key = base64.b64decode(encoding_aes_key + '=')
    except Exception as e:  # noqa: BLE001
        raise WeComCryptoError(f'invalid EncodingAESKey: {e}') from e
    if len(key) != 32:
        raise WeComCryptoError('EncodingAESKey must decode to 32 bytes')
    return key


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    amount = block - (len(data) % block)
    return data + bytes([amount]) * amount


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    amount = data[-1]
    if amount < 1 or amount > 32:
        raise WeComCryptoError('invalid padding')
    return data[:-amount]


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    items = sorted([str(token or ''), str(timestamp or ''), str(nonce or ''), str(encrypt or '')])
    return hashlib.sha1(''.join(items).encode()).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, encrypt: str, msg_signature: str) -> None:
    expected = signature(token, timestamp, nonce, encrypt)
    if not hmac_compare(expected, str(msg_signature or '')):
        raise WeComCryptoError('invalid msg_signature')


def hmac_compare(a: str, b: str) -> bool:
    import hmac as _hmac
    return _hmac.compare_digest(str(a or ''), str(b or ''))


def decrypt_message(encoding_aes_key: str, encrypt_b64: str) -> str:
    """encrypt 密文 → 明文 msg（剥离 random/receive_id）。"""
    key = _aes_key(encoding_aes_key)
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        dec = cipher.decryptor()
        plain = dec.update(base64.b64decode(encrypt_b64)) + dec.finalize()
    except Exception as e:  # noqa: BLE001
        raise WeComCryptoError(f'aes decrypt failed: {e}') from e
    plain = _pkcs7_unpad(plain)
    if len(plain) < 20:
        raise WeComCryptoError('decrypted payload too short')
    msg_len = struct.unpack('>I', plain[16:20])[0]
    if 20 + msg_len > len(plain):
        raise WeComCryptoError('msg_len exceeds payload')
    return plain[20:20 + msg_len].decode('utf-8', errors='replace')


def encrypt_message(encoding_aes_key: str, plain_msg: str, receive_id: str) -> str:
    """msg + receive_id → encrypt 密文（Base64）。"""
    key = _aes_key(encoding_aes_key)
    msg = plain_msg.encode('utf-8')
    rid = receive_id.encode('utf-8')
    payload = os.urandom(16) + struct.pack('>I', len(msg)) + msg + rid
    payload = _pkcs7_pad(payload)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    enc = cipher.encryptor()
    return base64.b64encode(enc.update(payload) + enc.finalize()).decode()


def parse_callback_xml(xml_text: str) -> dict:
    """回调 XML → {Encrypt, MsgSignature, Timestamp, Nonce}。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise WeComCryptoError(f'bad xml: {e}') from e
    out = {}
    for child in root:
        out[child.tag] = (child.text or '')
    return out


def build_encrypted_xml(encrypt: str, msg_signature: str, timestamp: str, nonce: str) -> str:
    return (
        '<xml>'
        f'<Encrypt><![CDATA[{encrypt}]]></Encrypt>'
        f'<MsgSignature><![CDATA[{msg_signature}]]></MsgSignature>'
        f'<Timestamp>{timestamp}</Timestamp>'
        f'<Nonce><![CDATA[{nonce}]]></Nonce>'
        '</xml>'
    )


def now_params() -> tuple:
    ts = str(int(time.time()))
    nonce = os.urandom(8).hex()
    return ts, nonce
