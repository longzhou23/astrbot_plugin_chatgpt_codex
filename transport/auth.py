from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .types import TransportAuthError, TransportNetworkError

OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part.encode("ascii")).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, IndexError, UnicodeError, json.JSONDecodeError):
        return {}


@dataclass(frozen=True, slots=True)
class AuthSnapshot:
    access_token: str
    refresh_token: str | None
    account_id: str | None
    email: str | None
    plan_type: str | None


class CodexAuthStore:
    """Small, redacting bridge to Codex's existing CODEX_HOME/auth.json.

    The raw credential values stay in local variables and are never returned by
    public metadata methods or written to logs.  Refresh persistence preserves
    the file shape used by the open-source Codex AuthManager.
    """

    def __init__(self, codex_home: Path, *, refresh_window: int = 120) -> None:
        self.codex_home = codex_home
        self.path = codex_home / "auth.json"
        self.refresh_window = max(0, refresh_window)
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TransportAuthError("CODEX_HOME 中没有 ChatGPT 登录态，请先登录") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TransportAuthError("CODEX_HOME/auth.json 无法读取") from exc
        if not isinstance(value, dict) or value.get("auth_mode") not in {
            "chatgpt",
            "chatgptAuthTokens",
        }:
            raise TransportAuthError("当前 CODEX_HOME 不是 ChatGPT OAuth 登录态")
        return value

    @staticmethod
    def _metadata(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None, int | None]:
        tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
        access = tokens.get("access_token") if isinstance(tokens.get("access_token"), str) else ""
        claims = _jwt_payload(access)
        id_token = tokens.get("id_token") if isinstance(tokens.get("id_token"), str) else ""
        id_claims = _jwt_payload(id_token)
        merged = {**claims, **id_claims}
        account_id = tokens.get("account_id") if isinstance(tokens.get("account_id"), str) else None
        account_id = account_id or merged.get("chatgpt_account_id") or merged.get("account_id")
        profile = merged.get("https://api.openai.com/profile")
        email = merged.get("email")
        if not email and isinstance(profile, dict):
            email = profile.get("email")
        plan = merged.get("chatgpt_plan_type") or merged.get("plan_type")
        exp = claims.get("exp") if isinstance(claims.get("exp"), int) else None
        return (
            account_id if isinstance(account_id, str) else None,
            email if isinstance(email, str) else None,
            plan if isinstance(plan, str) else None,
            exp,
        )

    async def snapshot(self, *, refresh: bool = True) -> AuthSnapshot:
        async with self._lock:
            payload = self._read()
            tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
            access = tokens.get("access_token") if isinstance(tokens.get("access_token"), str) else ""
            refresh_token = tokens.get("refresh_token") if isinstance(tokens.get("refresh_token"), str) else None
            account_id, email, plan, exp = self._metadata(payload)
            if not access:
                raise TransportAuthError("ChatGPT OAuth access token 不存在")
            if refresh and exp is not None and exp <= int(time.time()) + self.refresh_window:
                if not refresh_token:
                    raise TransportAuthError("ChatGPT access token 已过期且没有 refresh token")
                payload = await asyncio.to_thread(self._refresh_sync, payload, refresh_token)
                tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
                access = tokens.get("access_token") if isinstance(tokens.get("access_token"), str) else ""
                refresh_token = tokens.get("refresh_token") if isinstance(tokens.get("refresh_token"), str) else None
                account_id, email, plan, _ = self._metadata(payload)
            return AuthSnapshot(access, refresh_token, account_id, email, plan)

    def _refresh_sync(self, payload: dict[str, Any], refresh_token: str) -> dict[str, Any]:
        body = json.dumps(
            {
                "client_id": os.environ.get("CODEX_APP_SERVER_LOGIN_CLIENT_ID", OAUTH_CLIENT_ID),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8")
        request = Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Never include the response body: OAuth errors can echo credential material.
            raise TransportAuthError(f"ChatGPT OAuth 刷新失败（HTTP {exc.code}）") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportNetworkError("ChatGPT OAuth 刷新网络失败") from exc
        except (ValueError, UnicodeError) as exc:
            raise TransportAuthError("ChatGPT OAuth 刷新响应无效") from exc
        if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
            raise TransportAuthError("ChatGPT OAuth 刷新未返回 access token")
        updated = dict(payload)
        old_tokens = dict(payload.get("tokens") or {})
        old_tokens["access_token"] = result["access_token"]
        if isinstance(result.get("refresh_token"), str) and result["refresh_token"]:
            old_tokens["refresh_token"] = result["refresh_token"]
        if isinstance(result.get("id_token"), str) and result["id_token"]:
            old_tokens["id_token"] = result["id_token"]
        updated["tokens"] = old_tokens
        updated["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="auth.", suffix=".tmp", dir=self.codex_home)
        try:
            os.close(fd)
            Path(temp_name).write_text(
                json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return updated
