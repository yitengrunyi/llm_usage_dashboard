"""凭据注入单测 — 两条路径必须一致地把 TOTP 密钥送到 client。

注: 恢复自 origin/main (bd7cc65) 的可运行子集。TOTP 注入用例钉在本地尚未
移植的 2FA 功能上, 功能移植后从 origin/main 恢复完整版。
两条注入路径:
  - ingest/job.py:_inject_env_credentials  → 入库 (cron / backfill / 手动同步)
  - vendors.py:_hydrate                    → 主路由实时查询
"""
from __future__ import annotations

import pytest

from ingest.job import _inject_env_credentials
from vendors import _hydrate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NULLS_USERNAME", "NULLS_PASSWORD", "NULLS_TOTP_SECRET",
              "VERTEX_USERNAME", "VERTEX_PASSWORD", "VERTEX_TOTP_SECRET"):
        monkeypatch.delenv(k, raising=False)


class TestIngestPathInjection:
    def test_totp_secret_optional(self):
        """没开 2FA 的 vendor 不该因为缺这个 env 就炸。"""
        v = {"id": "vertex", "type": "new-api-direct"}

        _inject_env_credentials(v)

        assert not v.get("totp_secret")  # 空值 = 没配, _do_login 走无 2FA 分支


class TestRoutePathInjection:
    def test_hydrate_without_secret(self):
        out = _hydrate({"id": "nulls"})

        assert not out.get("totp_secret")
