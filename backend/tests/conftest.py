"""pytest 共享配置 — 把 backend/ 挂进 sys.path, 让测试能 import 顶层模块."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ingest.settings 在 import 期就读 DB_*, 单测不连库但要能 import — 给一组占位值.
# setdefault: 本地真配了 .env 就用真的, 不覆盖.
for _k, _v in {
    "DB_HOST": "localhost", "DB_PORT": "5432", "DB_NAME": "test",
    "DB_USER": "test", "DB_PASSWORD": "test",
}.items():
    os.environ.setdefault(_k, _v)
