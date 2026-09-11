"""newapi_direct_client 登录单测 — 覆盖登录协议适配与过期重登。

注: 恢复自 origin/main (9744b97/bd7cc65) 的可运行子集。2FA/TOTP 登录流与
异常响应诊断用例钉在本地尚未移植的功能上, 功能移植后从 origin/main 恢复完整版。
"""
from __future__ import annotations

import json

import pytest

import newapi_direct_client as nac


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is _INVALID_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


_INVALID_JSON = object()


class FakeSession:
    """按 URL 后缀返回预置响应, 并记录收到的 body。headers 模拟 requests.Session 的默认头。

    值可以是单个 FakeResponse, 或 list[FakeResponse] (按调用次序消费, 模拟
    "第一次 401 第二次成功" 这类时序)。GET/POST 共用同一张表。"""

    def __init__(self, responses: dict[str, FakeResponse | list]):
        self._responses = responses
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.headers: dict[str, str] = {}

    def _resolve(self, url: str) -> FakeResponse:
        for suffix, resp in self._responses.items():
            if url.split("?")[0].endswith(suffix):
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        raise AssertionError(f"unexpected request {url}")

    def post(self, url, json=None, timeout=None, **kwargs):
        self.posts.append((url, json or {}))
        return self._resolve(url)

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.gets.append(url)
        return self._resolve(url)


@pytest.fixture(autouse=True)
def _clear_session_cache():
    nac._session_cache.clear()
    yield
    nac._session_cache.clear()


def _patch_session(monkeypatch, session: FakeSession) -> None:
    monkeypatch.setattr(nac.requests, "Session", lambda: session)


LOGIN_OK = FakeResponse({
    "success": True, "message": "",
    "data": {"id": 42, "username": "bot", "role": 1, "status": 1},
})
# 新版 new-api (api7.xhub.chat 实测 2026-08): id 挪进 data.user, API 鉴权改用 access_token
LOGIN_OK_V2 = FakeResponse({
    "success": True, "message": "",
    "data": {
        "access_token": "AT-XYZ", "token_type": "Bearer",
        "access_expires_at": 1785000000, "session": "sess-1",
        "user": {"id": 4, "username": "bot", "role": 1},
    },
})


class TestPlainLogin:
    def test_old_protocol_does_not_set_bearer(self, monkeypatch):
        """老版协议 (vertex 等未升级实例) 靠 cookie 鉴权 — 不能凭空塞 Authorization 头。"""
        session = FakeSession({"/api/user/login": LOGIN_OK})
        _patch_session(monkeypatch, session)

        nac._do_login("https://newapi.example.com", "bot", "pw")

        assert "Authorization" not in session.headers


class TestNewProtocolLogin:
    """新版 new-api (2026-08 xhub 升级实测): data.user.id + access_token Bearer 鉴权。

    实测老鉴权 (cookie + New-Api-User) 已被上游拒: 401 invalid access token —
    登录后必须把 access_token 设为 session 默认头, 后续 logs/stat/data_self 全走它。
    """

    def test_sets_bearer_header_on_session(self, monkeypatch):
        session = FakeSession({"/api/user/login": LOGIN_OK_V2})
        _patch_session(monkeypatch, session)

        nac._do_login("https://api7.xhub.chat", "bot", "pw")

        assert session.headers.get("Authorization") == "Bearer AT-XYZ"

    def test_failed_login_reports_upstream_message(self, monkeypatch):
        _patch_session(monkeypatch, FakeSession({
            "/api/user/login": FakeResponse({"success": False, "message": "用户名或密码错误"}),
        }))

        with pytest.raises(PermissionError) as exc:
            nac._do_login("https://api7.xhub.chat", "bot", "pw")

        assert "用户名或密码错误" in str(exc.value)


class TestExpiredTokenRelogin:
    """access_token 过期时上游返英文 401 — 必须识别为登录态失效并自动重登。

    实测原文: {"success": false, "message": "Unauthorized, invalid access token"}
    老判定只认中文关键词 (未登录/过期/无权), 英文报错会被当成普通失败直接抛,
    session 缓存里的死 token 要等 30min TTL 才换 — 期间入库全红。
    """

    VENDOR = {"id": "nulls", "name": "nulls", "base_url": "https://api7.xhub.chat",
              "username": "bot", "password": "pw"}

    def test_chinese_401_still_recognized(self, monkeypatch):
        session = FakeSession({
            "/api/user/login": LOGIN_OK_V2,
            "/api/data/self": [
                FakeResponse({"success": False, "message": "未登录或登录已过期"}),
                FakeResponse({"success": True, "data": []}),
            ],
        })
        _patch_session(monkeypatch, session)

        result = nac.fetch_vendor_usage(self.VENDOR, "2026-07-31T00:00:00+08:00",
                                        "2026-07-31T23:59:59+08:00")

        assert result["total_cost"] == 0

    def test_non_auth_failure_does_not_relogin(self, monkeypatch):
        """普通业务失败 (非鉴权) 不该无脑重登刷上游 login 限流。"""
        session = FakeSession({
            "/api/user/login": LOGIN_OK_V2,
            "/api/data/self": FakeResponse({"success": False, "message": "internal error"}),
        })
        _patch_session(monkeypatch, session)

        with pytest.raises(RuntimeError):
            nac.fetch_vendor_usage(self.VENDOR, "2026-07-31T00:00:00+08:00",
                                   "2026-07-31T23:59:59+08:00")

        assert len([u for u, _ in session.posts if u.endswith("/login")]) == 1
