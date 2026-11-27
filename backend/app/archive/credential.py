"""迁移恢复凭证：服务端对"原归档号 + 版次"签发 HMAC 凭证，切片分析器据此读取可用样本。

凭证是自包含的可验证字符串（不依赖会话），同时落库一份；重启或回滚后仍可
用同一密钥核验真伪与适用范围（绑定原归档号，防止跨归档挪用）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

ISSUER = "exp-archive-migration"


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_credential(
    *, archive_no: str, version: str, ttl_days: int = 30, secret: str | None = None,
) -> tuple[str, str | None]:
    """签发凭证，返回 (凭证字符串, 过期时间 ISO)。"""
    secret = secret or settings.archive_credential_secret
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=ttl_days)
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "archive_no": archive_no,
        "version": version,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii").rstrip("=")
    signature = _sign(payload_b64, secret)
    credential = f"mrc.{payload_b64}.{signature}"
    return credential, expires.isoformat()


def _b64_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def verify_credential(
    credential: str, *, archive_no: str | None = None, secret: str | None = None,
) -> dict[str, Any]:
    """核验凭证，返回 {valid, reason, payload}。绑定归档号时校验适用范围。"""
    secret = secret or settings.archive_credential_secret
    parts = credential.split(".")
    if len(parts) != 3 or parts[0] != "mrc":
        return {"valid": False, "reason": "凭证格式非法", "payload": None}
    _, payload_b64, signature = parts
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(signature, expected):
        return {"valid": False, "reason": "凭证签名不匹配", "payload": None}
    try:
        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"valid": False, "reason": "凭证内容无法解析", "payload": None}
    if payload.get("iss") != ISSUER:
        return {"valid": False, "reason": "凭证签发方不匹配", "payload": payload}
    if archive_no is not None and payload.get("archive_no") != archive_no:
        return {"valid": False, "reason": "凭证与原归档号不匹配", "payload": payload}
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp < datetime.now(timezone.utc).timestamp():
        return {"valid": False, "reason": "凭证已过期", "payload": payload}
    return {"valid": True, "reason": "", "payload": payload}
