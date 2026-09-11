"""模型名 + 供应商归一化 (LiteLLM 模块).

跟 billing utils.normalize_model_name 同套规则 + LiteLLM 特有的 / 前缀处理:
- 削 / 前缀 (anthropic/, vertex_ai/, openrouter/google/ 等)
- 削已知 vendor 前缀 (openai- road- 等, 12 个)
- 削老 vendor.xxx 前缀 (anthropic. google. etc)
- 削日期后缀 (-20251101 / -2024-07-18 / -05-20)
- 削 context size (-200k -1m -128k)
- 削 lifecycle 标签 (-preview -exp -beta -ga -unlimit)
- 版本号统一 (4.6 → 4-6)
- gpt5 → gpt-5

provider 解析逻辑 (实测 Daily 数据驱动):
- 优先 model_group 前缀 (路由层信息最可信)
- model 路径前缀 (anthropic/ vertex_ai/ openrouter/ gemini/ bedrock/)
- custom_llm_provider (排除空 + 排除 "openai" 大量误标值)
- 国内模型名启发 (kimi → moonshot 等)
- 兜底 blueshirt
"""
from __future__ import annotations

import re

# 12 个真实供应商前缀 (来自实测 Daily 表 model_group 字段) — 用来从 model_group / model
# 自带 vendor 前缀里提取 provider. **不含** anthropic / gemini / google 这种"模型家族名",
# 那种走路径前缀映射 (_PATH_PREFIX_TO_VENDOR), 不直接当 vendor.
KNOWN_VENDORS: set[str] = {
    "vertex", "road", "wangsu", "tencent", "apevon", "ucloud",
    "blueshirt", "nulls", "openrouter", "rabyte", "moonshot",
    "openai",
    # 国内供应商前缀 (model 字段会直带, e.g. volcengine-doubao, deepseek-v4)
    "volcengine", "deepseek", "minimax",
}

# model 字段的 path 前缀 → 供应商
# (注意 openai/ 不映射, 因为大量是渠道转发 — 走 model_group 才知道真供应商)
_PATH_PREFIX_TO_VENDOR: dict[str, str] = {
    "anthropic": "blueshirt",
    "vertex_ai": "vertex",
    "vertex": "vertex",
    "openrouter": "openrouter",
    "gemini": "vertex",
    "bedrock": "blueshirt",
    "dashscope": "ali",
}

# 国内模型名前缀 → 供应商
_DOMESTIC_MODEL_PROVIDER: dict[str, str] = {
    "kimi":     "moonshot",
    "deepseek": "deepseek",
    "qwen":     "ali",
    "glm":      "zhipu",
    "doubao":   "volcengine",
}

_OLD_VENDOR_DOT_PREFIXES = ("anthropic.", "openai.", "google.", "bedrock.", "azure.", "vertex.")


def normalize_model_name(m: str | None) -> str:
    if not m:
        return ""
    s = m.strip()

    # 1. / 前缀全削 (openrouter/google/gemini-3 → gemini-3)
    while "/" in s:
        s = s.split("/", 1)[-1]

    # 2. vendor 前缀 (KNOWN_VENDORS + "-")
    while True:
        stripped = False
        low = s.lower()
        for v in KNOWN_VENDORS:
            pfx = f"{v}-"
            if low.startswith(pfx):
                s = s[len(pfx):]
                stripped = True
                break
        if not stripped:
            break

    # 3. 老的 vendor.xxx 前缀
    low = s.lower()
    for old in _OLD_VENDOR_DOT_PREFIXES:
        if low.startswith(old):
            s = s[len(old):]
            break

    # 4. gpt5 → gpt-5 / claude5 → claude-5 / gemini5 → gemini-5
    s = re.sub(r'^(gpt|claude|gemini)(\d)', r'\1-\2', s, flags=re.IGNORECASE)

    # 5. 日期后缀 (YYYYMMDD / YYYY-MM-DD / -MM-DD)
    s = re.sub(r'-20\d{6}', '', s)
    s = re.sub(r'-20\d{2}-\d{2}-\d{2}', '', s)
    s = re.sub(r'-\d{2}-\d{2}$', '', s)

    # 6. context size (-200k / -1m / -128k)
    s = re.sub(r'-\d+[km]\b', '', s, flags=re.IGNORECASE)

    # 7. lifecycle / 套餐标签
    s = re.sub(r'-unlimit\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'-(preview|exp|experimental|beta|alpha|ga)\b', '', s, flags=re.IGNORECASE)

    # 8. 版本号 4.6 → 4-6 (kimi-k2.5 → kimi-k2-5)
    s = re.sub(r'(\d+)\.(\d+)(?=\D|$)', r'\1-\2', s)

    # 9. 清掉多余 -
    s = re.sub(r'-+', '-', s).strip('-')

    return s


def extract_provider(
    model: str | None,
    model_group: str | None,
    custom_llm_provider: str | None,
) -> str:
    """实测数据驱动的 provider 优先级:

    1. model_group 前缀 (路由层信息最可信)
    2. model 字段 vendor 前缀 (`rabyte-claude-opus-4-6` 没 group 但 model 自带前缀)
    3. model 路径前缀 (`/` 前的部分) → _PATH_PREFIX_TO_VENDOR
    4. custom_llm_provider (排除空 / "openai" 误标值)
    5. 国内模型名启发 (kimi → moonshot 等)
    6. 兜底 "blueshirt" (国外模型默认走 blueshirt 渠道)
    """
    # 1. model_group 前缀
    if model_group:
        first = model_group.lower().split("-", 1)[0]
        if first in KNOWN_VENDORS:
            return first

    # 2. model 字段自带 vendor 前缀 (e.g. rabyte-claude-opus-4-6, openai-gpt-4.1)
    if model:
        low = model.lower()
        # 跳过 / 后再看 (path 后的 bare name 也可能带 vendor 前缀)
        bare = low.split("/")[-1] if "/" in low else low
        first = bare.split("-", 1)[0]
        if first in KNOWN_VENDORS:
            return first

    # 3. model 路径前缀
    if model and "/" in model:
        first = model.lower().split("/", 1)[0]
        if first in _PATH_PREFIX_TO_VENDOR:
            return _PATH_PREFIX_TO_VENDOR[first]

    # 4. custom_llm_provider (排除空 + 误标的 "openai")
    if custom_llm_provider:
        cleaned = custom_llm_provider.strip().lower()
        if cleaned and cleaned != "openai":
            # 4a. "anthropic" / "vertex_ai" / "gemini" / "bedrock" 这种"模型家族/平台"名,
            # 跟 path 前缀走同一套映射 (anthropic 直连 → blueshirt 渠道, gemini → vertex 等)
            if cleaned in _PATH_PREFIX_TO_VENDOR:
                return _PATH_PREFIX_TO_VENDOR[cleaned]
            # 4b. 真实渠道名 (volcengine / deepseek / moonshot 等)
            if cleaned in KNOWN_VENDORS:
                return cleaned
            # 4c. 国内供应商映射 (kimi → moonshot 这种)
            if cleaned in set(_DOMESTIC_MODEL_PROVIDER.values()):
                return cleaned
            # 4d. 其它未知值 (azure / mistral / xai 这种没在 KNOWN_VENDORS 的) 不接受,
            # 落 step 5/6 — 不让上游 raw 值污染下拉

    # 5. 国内模型名启发 (削掉可能的 vendor 前缀后看 bare model)
    bare_model = (model or "").lower()
    if "/" in bare_model:
        bare_model = bare_model.split("/", 1)[-1]
    for v in KNOWN_VENDORS:
        pfx = f"{v}-"
        if bare_model.startswith(pfx):
            bare_model = bare_model[len(pfx):]
            break
    for prefix, vendor in _DOMESTIC_MODEL_PROVIDER.items():
        if bare_model.startswith(prefix):
            return vendor

    # 6. 兜底: 国外模型走 blueshirt
    return "blueshirt"


# ─────────────────────────────────────────────────────────────────────
# 兼容旧导入: service 层多处 import 这些名字, 不破坏调用方
# ─────────────────────────────────────────────────────────────────────
KNOWN_PROVIDERS = KNOWN_VENDORS
PROVIDER_PREFIXES = tuple(f"{v}-" for v in KNOWN_VENDORS)
DOMESTIC_MODEL_PROVIDER_MAP = _DOMESTIC_MODEL_PROVIDER
normalized_model_name = normalize_model_name
