"""Official Sign in with ChatGPT, with Windows-protected local credentials.

Reference: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
No API keys, ChatGPT cookies, or credentials belonging to another application.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import math
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt

APP_NAME = "GPT Chat Helper"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
USAGE_URL = "https://chatgpt.com/settings/usage"
_THREAD_LOCK = threading.RLock()
_TERMINAL_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
}


class AuthError(RuntimeError):
    """Messages are safe to display; HTTP bodies and bearer tokens are never logged."""
    pause_capture = False


def api_error(status: int | None, code: str | None = None) -> AuthError:
    messages = {
        "subscription_sharing_user_not_eligible": "当前账号或工作区未获准使用 ChatGPT 订阅额度。",
        "subscription_sharing_usage_limit_exceeded": "此应用的 ChatGPT 使用额度已达到上限，请在“管理用量”中查看。",
        "subscription_sharing_usage_unavailable": "暂时无法查询订阅额度，请稍后再试。",
        "subscription_sharing_user_unavailable": "账号服务暂时不可用，请稍后再试。",
        "subscription_sharing_unsupported_capability": "当前账号或模型不支持这项请求，请刷新模型列表并选择其他模型。",
        "subscription_sharing_route_not_supported": "当前账号尚未开放此订阅调用方式。",
        "subscription_sharing_invalid_user": "订阅身份校验失败，请在设置中重新登录。",
        "chatpass_v2_scope_not_authorized": "订阅使用授权不足，请在设置中重新授权。",
        "chatpass_v2_invalid_authorization_context": "订阅授权无效，请在设置中重新授权。",
    }
    message = messages.get(code or "") or {
        400: "请求未被接受，请尝试刷新模型列表并选择其他模型。",
        401: "登录凭据或订阅授权未被接受，请在设置中重新登录。",
        403: "OpenAI 拒绝了访问，可能受账号、工作区或服务地区限制。",
        429: "请求受到限流，请在“管理用量”中查看额度并稍后重试。",
        503: "ChatGPT 订阅调用暂时不可用，请稍后再试。",
    }.get(status, "连接 OpenAI 失败，请检查网络后重试。")
    # Only known machine-readable codes are displayed, never an arbitrary server message.
    suffix = f"（HTTP {status}" if status else "（"
    if code in messages:
        suffix += (" / " if status else "") + code
    error = AuthError(message + (suffix + "）" if status or code in messages else ""))
    error.pause_capture = status in (400, 401, 403, 429) or code in {
        "subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible",
        "subscription_sharing_invalid_user", "subscription_sharing_unsupported_capability",
        "subscription_sharing_route_not_supported", "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context",
    }
    return error


def _protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise AuthError("此版本使用 Windows 账号保护登录凭据，请在 Windows 中运行。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), APP_NAME, None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise AuthError("Windows 无法读取或保护此账号的登录凭据，请重新登录。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


class AuthManager:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GPTChatHelper"
        self.credentials = self.directory / "credentials.bin"

    @contextlib.contextmanager
    def locked(self):
        """Serialize rotating refresh tokens across threads and application instances."""
        import msvcrt
        with _THREAD_LOCK:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "session.lock").open("a+b") as handle:
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    raise AuthError("另一个助手正在更新登录状态，请稍后重试。") from None
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _load(self) -> dict:
        if not self.credentials.exists():
            return {"accounts": [], "active": None}
        try:
            data = json.loads(_protect(self.credentials.read_bytes(), decrypt=True))
            if not isinstance(data.get("accounts"), list):
                raise ValueError
            return data
        except (ValueError, OSError):
            raise AuthError("登录记录无法读取，请在设置中重新登录。") from None

    def _save(self, data: dict):
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.directory / ("credentials." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_bytes(_protect(json.dumps(data, ensure_ascii=False).encode("utf-8")))
            os.replace(temp, self.credentials)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _account(data: dict, client_id: str | None = None) -> dict | None:
        selected = client_id or data.get("active")
        return next((p for p in data["accounts"] if p["client_id"] == selected), None)

    def status(self) -> dict:
        try:
            data = self._load()
            profile = self._account(data)
        except AuthError:
            return {"connected": False, "enabled": False, "email": "", "client_id": None}
        return {
            "connected": bool(profile and (profile.get("access_token") or profile.get("refresh_token"))),
            "enabled": bool(profile and PLAN_SCOPE in profile.get("scopes", []) and
                            (profile.get("access_token") or profile.get("refresh_token"))),
            "email": profile.get("email", "") if profile else "",
            "client_id": profile.get("client_id") if profile else None,
        }

    def accounts(self) -> list[dict]:
        try:
            return [{"client_id": p["client_id"], "email": p.get("email", "ChatGPT 账号")}
                    for p in self._load()["accounts"]]
        except AuthError:
            return []

    def select(self, client_id: str):
        with self.locked():
            data = self._load()
            if not self._account(data, client_id):
                raise AuthError("找不到此账号，请重新登录。")
            data["active"] = client_id
            self._save(data)

    def host_id(self) -> str:
        with self.locked():
            path = self.directory / "host.json"
            if path.exists():
                try:
                    value = json.loads(path.read_text("utf-8"))["ext_agent_host_id"]
                    uuid.UUID(value.removeprefix("urn:uuid:"))
                    return value
                except (ValueError, KeyError):
                    raise AuthError("设备标识无法读取，请检查本地登录目录。") from None
            value = "urn:uuid:" + str(uuid.uuid4())
            path.write_text(json.dumps({"ext_agent_host_id": value}), encoding="utf-8")
            return value

    @staticmethod
    def discovery() -> dict:
        try:
            response = httpx.get(ISSUER + "/.well-known/openid-configuration", timeout=20)
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("issuer") != ISSUER:
                raise ValueError
            for field in ("jwks_uri", "revocation_endpoint"):
                url = urlparse(metadata[field])
                if url.scheme != "https" or url.netloc != "auth.openai.com":
                    raise ValueError
            return metadata
        except (httpx.HTTPError, ValueError, KeyError):
            raise AuthError("无法获取 OpenAI 登录服务信息，请检查网络后重试。") from None

    @staticmethod
    def validate_identity(id_token: str, client_id: str, nonce: str | None = None) -> dict:
        try:
            metadata = AuthManager.discovery()
            response = httpx.get(metadata["jwks_uri"], timeout=20)
            response.raise_for_status()
            header = jwt.get_unverified_header(id_token)
            keys = jwt.PyJWKSet.from_dict(response.json())
            key = next(k for k in keys.keys if k.key_id == header.get("kid"))
            claims = jwt.decode(id_token, key.key, algorithms=["RS256"],
                                audience=client_id, issuer=ISSUER, leeway=30,
                                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError
            return claims
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, StopIteration):
            raise AuthError("OpenAI 登录身份校验失败，请重新发起登录。") from None

    @staticmethod
    def _post_token(form: dict) -> dict:
        try:
            response = httpx.post(TOKEN_URL, data=form, timeout=30)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError("登录服务连接失败，请检查网络后重试。") from None
        if response.status_code != 200:
            raw_error = body.get("error")
            code = raw_error.get("code") if isinstance(raw_error, dict) else raw_error
            error = AuthError("登录凭据已失效，请在设置中重新登录。" if code in _TERMINAL_REFRESH
                              else "OpenAI 登录服务拒绝了请求，请重新登录或稍后重试。")
            error.terminal = code in _TERMINAL_REFRESH
            raise error
        try:
            if not body.get("access_token") or body.get("token_type", "Bearer").lower() != "bearer":
                raise ValueError
            if not math.isfinite(float(body["expires_in"])) or float(body["expires_in"]) <= 0:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise AuthError("登录服务返回了不完整的凭据，请重新登录。") from None
        return body

    @staticmethod
    def _update_tokens(profile: dict, tokens: dict):
        profile["access_token"] = tokens["access_token"]
        profile["expires_at"] = time.time() + float(tokens["expires_in"])
        for name in ("refresh_token", "id_token", "earliest_refresh_at"):
            if name in tokens:
                profile[name] = tokens[name]
        if "scope" in tokens:
            profile["scopes"] = tokens["scope"].split()

    def access_token(self, expected_client_id: str | None = None) -> str:
        with self.locked():
            data = self._load()
            profile = self._account(data)
            if not profile or not (profile.get("access_token") or profile.get("refresh_token")):
                raise AuthError("请先在设置中登录 ChatGPT。")
            if expected_client_id and profile["client_id"] != expected_client_id:
                raise AuthError("账号已切换，请重新生成回复。")
            if PLAN_SCOPE not in profile.get("scopes", []):
                raise AuthError("已登录，但未授权使用订阅额度，请在设置中点击“授权订阅额度”。")
            if profile.get("access_token") and profile.get("expires_at", 0) > time.time() + 60:
                return profile["access_token"]
            if not profile.get("refresh_token"):
                raise AuthError("登录已过期，请在设置中重新登录。")
            try:
                tokens = self._post_token({
                    "grant_type": "refresh_token", "client_id": profile["client_id"],
                    "refresh_token": profile["refresh_token"], "resource": RESOURCE,
                })
            except AuthError as error:
                if getattr(error, "terminal", False):
                    self._clear_tokens(profile)
                    self._save(data)
                    error.pause_capture = True
                raise
            if tokens.get("id_token"):
                claims = self.validate_identity(tokens["id_token"], profile["client_id"])
                if claims["sub"] != profile["subject"]:
                    raise AuthError("刷新后的账号身份不匹配，请重新登录。")
            self._update_tokens(profile, tokens)
            self._save(data)
            if PLAN_SCOPE not in profile.get("scopes", []):
                raise AuthError("订阅使用授权已失效，请在设置中重新授权。")
            return profile["access_token"]

    def new_login(self, client_id: str | None = None, consent: bool = False,
                  pending_client_id: str | None = None) -> "LoginFlow":
        data = self._load()
        profile = self._account(data, client_id) if client_id else None
        if client_id and not profile:
            raise AuthError("找不到所选账号，请选择“添加账号”。")
        return LoginFlow(self, dict(profile) if profile else None, consent, pending_client_id)

    @staticmethod
    def _clear_tokens(profile: dict):
        for name in ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at", "scopes"):
            profile.pop(name, None)

    def sign_out(self) -> str:
        confirmed = True
        with self.locked():
            data = self._load()
            profile = self._account(data)
            if not profile:
                return "已退出登录。"
            if profile.get("refresh_token"):
                try:
                    endpoint = self.discovery()["revocation_endpoint"]
                    for attempt in range(2):
                        response = httpx.post(endpoint, data={
                            "token": profile["refresh_token"], "token_type_hint": "refresh_token",
                            "client_id": profile["client_id"],
                        }, timeout=15)
                        if response.status_code < 500:
                            break
                        time.sleep(0.5)
                    confirmed = response.status_code == 200
                except (httpx.HTTPError, AuthError):
                    confirmed = False
            self._clear_tokens(profile)
            self._save(data)
        return ("已退出登录。" if confirmed else
                "已在本机退出；远程撤销未确认，可在 ChatGPT 设置中断开此应用。")

    def list_models(self) -> list[dict]:
        profile_id = self.status()["client_id"]
        token = self.access_token(profile_id)
        try:
            response = httpx.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + token}, timeout=30)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthError("获取 ChatGPT 模型失败，请检查网络后重试。") from None
        if response.status_code != 200:
            error = body.get("error")
            raise api_error(response.status_code, error.get("code") if isinstance(error, dict) else None)
        models = [{"slug": m["slug"], "display_name": m.get("display_name") or m["slug"]}
                  for m in body.get("models", []) if m.get("visibility") == "list" and m.get("slug")]
        if not models:
            raise AuthError("此账号未返回可用的 ChatGPT 模型，请检查订阅授权。")
        if self.status()["client_id"] != profile_id:
            raise AuthError("账号已切换，请重新刷新模型列表。")
        return models


class LoginFlow:
    def __init__(self, manager: AuthManager, profile: dict | None, consent: bool,
                 pending_client_id: str | None = None):
        self.manager, self.profile, self.consent = manager, profile, consent
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.cancelled = threading.Event()
        self.result: dict | None = None
        self.client_id = profile["client_id"] if profile else (pending_client_id or "dynamic_agent_client")

    def cancel(self):
        self.cancelled.set()

    def authorization_url(self, redirect_uri: str) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        params = {
            "client_id": self.client_id, "ext_agent_host_id": self.manager.host_id(),
            "response_type": "code", "redirect_uri": redirect_uri,
            "scope": SCOPES, "resource": RESOURCE, "state": self.state,
            "nonce": self.nonce, "code_challenge_method": "S256", "code_challenge": challenge,
        }
        if self.client_id == "dynamic_agent_client":
            params["agent_name_hint"] = APP_NAME
        elif self.profile:
            if self.profile.get("id_token"):
                params["id_token_hint"] = self.profile["id_token"]
            if self.profile.get("email"):
                params["login_hint"] = self.profile["email"]
        if self.consent:
            params["prompt"] = "consent"
        return AUTHORIZE_URL + "?" + urlencode(params)

    def accept_callback(self, query: str) -> bool:
        values = parse_qs(query)
        if any(len(items) != 1 for items in values.values()):
            return False
        result = {name: items[0] for name, items in values.items()}
        if not secrets.compare_digest(result.get("state", "").encode("utf-8"), self.state.encode("ascii")):
            return False
        if result.get("error"):
            self.result = {"error": "登录或订阅授权已取消。"}
            return True
        issued = result.get("client_id") or (self.client_id if self.client_id != "dynamic_agent_client" else "")
        if not result.get("code") or not issued or issued == "dynamic_agent_client":
            self.result = {"error": "授权注册未完成，请重新登录。"}
        elif self.client_id != "dynamic_agent_client" and issued != self.client_id:
            self.result = {"error": "授权回调中的账号注册不匹配，请重新登录。"}
        else:
            self.result = {"code": result["code"], "client_id": issued}
        return True

    def run(self) -> dict:
        flow = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Authorization codes and token hints must never appear in HTTP logs.

            def do_GET(self):
                url = urlparse(self.path)
                accepted = url.path == "/auth/callback" and flow.accept_callback(url.query)
                self.send_response(200 if accepted else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                message = "授权回调已收到，请回到 GPT 聊天助手查看登录结果。" if accepted else "无效的登录回调。"
                self.wfile.write(("<!doctype html><meta charset='utf-8'><title>GPT Chat Helper</title><p>" + message + "</p>").encode("utf-8"))

        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            server.timeout = 0.25
            redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
            url = self.authorization_url(redirect_uri)
            if not webbrowser.open(url):
                raise AuthError("无法打开系统浏览器，请检查默认浏览器后重试。")
            deadline = time.monotonic() + 300
            while not self.result and not self.cancelled.is_set() and time.monotonic() < deadline:
                server.handle_request()
            if self.cancelled.is_set():
                raise AuthError("登录已取消。")
            if not self.result:
                raise AuthError("登录等待超时，请再次点击 Continue with ChatGPT。")
        if "error" in self.result:
            raise AuthError(self.result["error"])
        client_id = self.result["client_id"]
        # Retained on this pending flow if exchanging an expired code fails.
        self.client_id = client_id
        tokens = self.manager._post_token({
            "grant_type": "authorization_code", "client_id": client_id,
            "code": self.result["code"], "code_verifier": self.verifier,
            "redirect_uri": redirect_uri, "resource": RESOURCE,
        })
        claims = self.manager.validate_identity(tokens.get("id_token", ""), client_id, self.nonce)
        if self.profile and claims["sub"] != self.profile["subject"]:
            raise AuthError("登录身份与所选账号不匹配，请选择“添加账号”。")
        if self.cancelled.is_set():
            raise AuthError("登录已取消。")
        profile = dict(self.profile or {})
        profile.update(client_id=client_id, subject=claims["sub"], issuer=ISSUER,
                       email=claims.get("email", "ChatGPT 账号"), ext_agent_host_id=self.manager.host_id())
        self.manager._update_tokens(profile, tokens)
        with self.manager.locked():
            data = self.manager._load()
            data["accounts"] = [p for p in data["accounts"] if p["client_id"] != client_id] + [profile]
            data["active"] = client_id
            self.manager._save(data)
        return self.manager.status()


auth = AuthManager()
