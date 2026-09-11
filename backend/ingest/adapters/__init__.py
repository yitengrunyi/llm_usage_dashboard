"""adapter 注册 dict — vendor_id → Adapter 类. PR2~PR3d 逐个填.

job.py 通过 ADAPTERS[vendor_id](vendor_config) 实例化对应 adapter.
"""
from __future__ import annotations

from ingest.adapters.base import ModelRow, VendorAdapter
from ingest.adapters.apevon_adapter import ApevonAdapter
from ingest.adapters.bigmodel_adapter import BigModelAdapter
from ingest.adapters.blueshirt_adapter import BlueshirtAdapter
from ingest.adapters.grok_adapter import GrokAdapter
from ingest.adapters.kimi_adapter import KimiAdapter
from ingest.adapters.newapi_direct_adapter import XhubAdapter
from ingest.adapters.openai_adapter import OpenAIAdapter
from ingest.adapters.road2all_adapter import Road2AllAdapter
from ingest.adapters.tencent_adapter import TencentAdapter
from ingest.adapters.ucloud_adapter import UCloudAdapter
from ingest.adapters.volcengine_adapter import VolcengineAdapter
from ingest.adapters.wangsu_adapter import WangsuAdapter

ADAPTERS: dict[str, type[VendorAdapter]] = {
    "openai": OpenAIAdapter,
    "tencent": TencentAdapter,
    "wangsu": WangsuAdapter,
    "kimi": KimiAdapter,
    "volcengine": VolcengineAdapter,
    "apevon": ApevonAdapter,
    "bigmodel": BigModelAdapter,
    "blueshirt": BlueshirtAdapter,
    "nulls": XhubAdapter,
    "road2all": Road2AllAdapter,
    "ucloud": UCloudAdapter,
    "grok": GrokAdapter,
}

__all__ = ["ADAPTERS", "ModelRow", "VendorAdapter"]
