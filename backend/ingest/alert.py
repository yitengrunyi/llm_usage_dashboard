"""飞书 webhook 告警 — 参考 claude-pool-lite 风格简化版.

只一种用途: 入库失败推送. webhook 没配就降级本地日志, 不抛错.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("ingest.alert")

WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
NODE_ID = os.environ.get("INGEST_NODE_ID", "").strip()  # 多点部署区分

CST = timezone(timedelta(hours=8))


def _send_card(title: str, md_body: str, template: str = "red") -> bool:
    """飞书 interactive 卡片. webhook 空时降级本地日志, 不抛错."""
    if NODE_ID:
        title = f"{title} @{NODE_ID}"

    if not WEBHOOK_URL:
        log.warning(f"[飞书 — 未配 webhook, 仅日志] {title}\n{md_body}")
        return False

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md_body}},
            ],
        },
    }

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=5)
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (0, None):
            log.error(f"飞书推送失败 (code != 0): {data}")
            return False
        log.info(f"飞书推送 ✓: {title}")
        return True
    except Exception as e:
        log.error(f"飞书推送异常 (不影响主流程): {e}")
        return False


def feishu_failure(vendor_id: str, start: str, end: str, error: str,
                    run_id: int | None = None, attempt: int = 1) -> bool:
    """retry 用尽后调一次. webhook 没配返 False 但不抛."""
    title = f"⚠️ 入库失败 - {vendor_id}"
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    md = (
        f"**{vendor_id}** 入库失败 (重试 {attempt} 次仍未成功)\n"
        f"时间窗: `{start} ~ {end}`\n"
        f"错误: \n```\n{(error or '')[:500]}\n```\n"
    )
    if run_id is not None:
        md += f"Run ID: `{run_id}` (调 `POST /api/ingest/runs/{run_id}/retry` 重试)\n"
    md += f"时间: {now}"

    return _send_card(title, md, template="red")
