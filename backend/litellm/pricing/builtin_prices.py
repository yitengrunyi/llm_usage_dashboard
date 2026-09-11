"""
内置定价（唯一来源）：计费仅以此为准，不再查 DB。
单位 USD/1M tokens。支持 standard（input/output/cache_read）与 advanced（阶梯 + 缓存写/读）。
新增模型直接在此 dict 中追加即可。
"""
from __future__ import annotations

BUILTIN_STANDARD_PRICES: dict[str, dict] = {
    # GPT 系列（美元）
    "gpt-4": {"type": "with_cache", "currency": "USD", "input_per_1m": 2.0, "output_per_1m": 8.0, "cache_read_per_1m": 0.5},
    # gpt-4.1 独立定价（与 gpt-4 相同，但独立配置以便未来调整）
    "gpt-4.1": {"type": "with_cache", "currency": "USD", "input_per_1m": 2.0, "output_per_1m": 8.0, "cache_read_per_1m": 0.5},
    "gpt-4.1-mini": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.4, "output_per_1m": 1.6, "cache_read_per_1m": 0.1},
    "gpt-4.1-nano": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.1, "output_per_1m": 0.4, "cache_read_per_1m": 0.025},
    "gpt-4o": {"type": "with_cache", "currency": "USD", "input_per_1m": 2.5, "output_per_1m": 10.0, "cache_read_per_1m": 1.25},
    "gpt-4o-mini": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.15, "output_per_1m": 0.6, "cache_read_per_1m": 0.075},
    "gpt-5": {"type": "with_cache", "currency": "USD", "input_per_1m": 1.25, "output_per_1m": 10.0, "cache_read_per_1m": 0.125},
    "gpt-5-chat": {"type": "with_cache", "currency": "USD", "input_per_1m": 1.25, "output_per_1m": 10.0, "cache_read_per_1m": 0.125},
    "gpt-5-mini": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.25, "output_per_1m": 2.0, "cache_read_per_1m": 0.025},
    "gpt-5-nano": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.05, "output_per_1m": 0.4, "cache_read_per_1m": 0.005},
    # gpt-5.1 独立定价（与 gpt-5 相同，但独立配置以便未来调整）
    "gpt-5.1": {"type": "with_cache", "currency": "USD", "input_per_1m": 1.25, "output_per_1m": 10.0, "cache_read_per_1m": 0.125},
    "gpt-5.2": {"type": "with_cache", "currency": "USD", "input_per_1m": 1.75, "output_per_1m": 14.0, "cache_read_per_1m": 0.175},
    "o3": {"type": "with_cache", "currency": "USD", "input_per_1m": 2.0, "output_per_1m": 8.0, "cache_read_per_1m": 0.5},
    "o4-mini": {"type": "with_cache", "currency": "USD", "input_per_1m": 1.1, "output_per_1m": 4.4, "cache_read_per_1m": 0.275},
    # Claude 系列（美元）
    "claude-opus-4-6": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 5}, {"up_to_k": None, "price_per_1m": 10}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 25}, {"up_to_k": None, "price_per_1m": 37.5}],
        "cache_write_tiers": [{"up_to_k": 200, "price_per_1m": 6.25}, {"up_to_k": None, "price_per_1m": 12.5}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.5}, {"up_to_k": None, "price_per_1m": 1}],
    },
    # Claude Sonnet 4.6：200K 阶梯
    "claude-sonnet-4-6": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 3}, {"up_to_k": None, "price_per_1m": 6}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 15}, {"up_to_k": None, "price_per_1m": 22.5}],
        "cache_write_tiers": [{"up_to_k": 200, "price_per_1m": 3.75}, {"up_to_k": None, "price_per_1m": 7.5}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.3}, {"up_to_k": None, "price_per_1m": 0.6}],
    },
    # Kimi K2 系列
    "kimi-k2.5": {
        "type": "with_cache",
        "currency": "元",
        "input_per_1m": 4.0,
        "output_per_1m": 21.0,
        "cache_read_per_1m": 0.7,
    },
    "kimi-k2-0711-preview": {
        "type": "with_cache",
        "currency": "元",
        "input_per_1m": 4,
        "output_per_1m": 16,
        "cache_read_per_1m": 1,
    },
    "kimi-k2-thinking": {
        "type": "with_cache",
        "currency": "元",
        "input_per_1m": 4,
        "output_per_1m": 16,
        "cache_read_per_1m": 1,
    },
    "kimi-k2-thinking-turbo": {
        "type": "with_cache",
        "currency": "元",
        "input_per_1m": 8,
        "output_per_1m": 58,
        "cache_read_per_1m": 1,
    },
    "kimi-k2-turbo": {
        "type": "with_cache",
        "currency": "元",
        "input_per_1m": 8,
        "output_per_1m": 58,
        "cache_read_per_1m": 1,
    },

    # Qwen-Long：Batch 折扣价格（无 cache）
    "qwen-long": {
        "type": "standard",
        "currency": "元",
        "input_per_1m": 0.5,
        "output_per_1m": 2,
        # 0.5 是 batch 折扣，没有 cache_read
    },

    # Qwen-Plus：阶梯定价 + Thinking
    # 统一 input: (0, 128K] → 0.8, (128K, 256K] → 2.4, (256K, 1024K] → 4.8
    # nothinking output: (0, 128K] → 2, (128K, 256K] → 20, (256K, 1024K] → 48
    # thinking output: (0, 128K] → 8, (128K, 256K] → 24, (256K, 1024K] → 64
    # 无 cache（0.5 是 batch 折扣但不计费）
    "qwen-plus": {
        "type": "advanced",
        "currency": "元",
        # 统一 input 阶梯
        "input_tiers": [
            {"up_to_k": 128, "price_per_1m": 0.8},
            {"up_to_k": 256, "price_per_1m": 2.4},
            {"up_to_k": 1024, "price_per_1m": 4.8},
            {"up_to_k": None, "price_per_1m": 4.8}
        ],
        # nothinking output 阶梯
        "output_tiers": [
            {"up_to_k": 128, "price_per_1m": 2},
            {"up_to_k": 256, "price_per_1m": 20},
            {"up_to_k": 1024, "price_per_1m": 48},
            {"up_to_k": None, "price_per_1m": 48}
        ],
        # thinking output 阶梯
        "thinking_tiers": [
            {"up_to_k": 128, "price_per_1m": 8},
            {"up_to_k": 256, "price_per_1m": 24},
            {"up_to_k": 1024, "price_per_1m": 64},
            {"up_to_k": None, "price_per_1m": 64}
        ],
    },

    # Qwen-Plus-Latest：与 qwen-plus 相同（无 cache）
    "qwen-plus-latest": {
        "type": "advanced",
        "currency": "元",
        # 统一 input 阶梯
        "input_tiers": [
            {"up_to_k": 128, "price_per_1m": 0.8},
            {"up_to_k": 256, "price_per_1m": 2.4},
            {"up_to_k": 1024, "price_per_1m": 4.8},
            {"up_to_k": None, "price_per_1m": 4.8}
        ],
        # nothinking output 阶梯
        "output_tiers": [
            {"up_to_k": 128, "price_per_1m": 2},
            {"up_to_k": 256, "price_per_1m": 20},
            {"up_to_k": 1024, "price_per_1m": 48},
            {"up_to_k": None, "price_per_1m": 48}
        ],
        # thinking output 阶梯
        "thinking_tiers": [
            {"up_to_k": 128, "price_per_1m": 8},
            {"up_to_k": 256, "price_per_1m": 24},
            {"up_to_k": 1024, "price_per_1m": 64},
            {"up_to_k": None, "price_per_1m": 64}
        ],
    },

    # Qwen2.5-72B-Instruct：固定单价
    "qwen2.5-72b-instruct": {
        "type": "standard",
        "currency": "元",
        "input_per_1m": 4,
        "output_per_1m": 12,
    },

    # Qwen2.5-VL-72B-Instruct：固定单价
    "qwen2.5-vl-72b-instruct": {
        "type": "standard",
        "currency": "元",
        "input_per_1m": 4,
        "output_per_1m": 12,
    },

    # Qwen3-0.6B：Thinking 定价
    "qwen3-0.6b": {
        "type": "with_thinking",
        "currency": "元",
        "input_per_1m": 0.3,
        "output_per_1m": 3,        # nothinking
        "thinking_per_1m": 1.2,    # thinking
    },

    # Qwen3-Max：阶梯定价 + 缓存读写
    # input: (0,32K] → 2.5, (32K,128K] → 4, (128K,256K] → 7
    # output: (0,32K] → 10, (32K,128K] → 16, (128K,256K] → 28
    # cache_write (隐式): 2.5, 4, 7 | cache_read (隐式): 0.5, 0.8, 1.4
    # cache_write (显式): 3.125, 4.5, 8.75 | cache_read (显式): 0.25, 0.4, 0.7
    # 注：暂不区分隐式/显式，使用隐式定价
    "qwen3-max": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [
            {"up_to_k": 32, "price_per_1m": 2.5},
            {"up_to_k": 128, "price_per_1m": 4},
            {"up_to_k": 256, "price_per_1m": 7},
            {"up_to_k": None, "price_per_1m": 7}
        ],
        "output_tiers": [
            {"up_to_k": 32, "price_per_1m": 10},
            {"up_to_k": 128, "price_per_1m": 16},
            {"up_to_k": 256, "price_per_1m": 28},
            {"up_to_k": None, "price_per_1m": 28}
        ],
        "cache_write_tiers": [
            {"up_to_k": 32, "price_per_1m": 2.5},
            {"up_to_k": 128, "price_per_1m": 4},
            {"up_to_k": 256, "price_per_1m": 7},
            {"up_to_k": None, "price_per_1m": 7}
        ],
        "cache_read_tiers": [
            {"up_to_k": 32, "price_per_1m": 0.5},
            {"up_to_k": 128, "price_per_1m": 0.8},
            {"up_to_k": 256, "price_per_1m": 1.4},
            {"up_to_k": None, "price_per_1m": 1.4}
        ],
    },

    # GLM-4.5 阶梯定价
    # (0, 32K]: input=3, output=14
    # (32K, 96K]: input=4, output=16
    "glm-4.5": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [
            {"up_to_k": 32, "price_per_1m": 3},
            {"up_to_k": 96, "price_per_1m": 4},
            {"up_to_k": None, "price_per_1m": 4}
        ],
        "output_tiers": [
            {"up_to_k": 32, "price_per_1m": 14},
            {"up_to_k": 96, "price_per_1m": 16},
            {"up_to_k": None, "price_per_1m": 16}
        ],
    },
    "glm-4.5-thinking": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [
            {"up_to_k": 32, "price_per_1m": 3},
            {"up_to_k": 96, "price_per_1m": 4},
            {"up_to_k": None, "price_per_1m": 4}
        ],
        "output_tiers": [
            {"up_to_k": 32, "price_per_1m": 14},
            {"up_to_k": 96, "price_per_1m": 16},
            {"up_to_k": None, "price_per_1m": 16}
        ],
    },

    # Claude Sonnet 4.5 / Sonnet 4：固定单价
    "claude-sonnet-4-5": {
        "type": "with_cache",
        "currency": "USD",
        "input_per_1m": 3,
        "output_per_1m": 15,
        "cache_write_per_1m": 3.75,
        "cache_read_per_1m": 0.3,
    },
    "claude-sonnet-4": {
        "type": "with_cache",
        "currency": "USD",
        "input_per_1m": 3,
        "output_per_1m": 15,
        "cache_write_per_1m": 3.75,
        "cache_read_per_1m": 0.3,
    },
    # Claude Haiku 4.5：固定单价
    "claude-haiku-4-5": {
        "type": "with_cache",
        "currency": "USD",
        "input_per_1m": 1,
        "output_per_1m": 5,
        "cache_write_per_1m": 1.25,
        "cache_read_per_1m": 0.1,
    },
    # Gemini 2.5 Pro Preview（暂无写，仅输入/输出）
    "gemini-2.5-pro-preview": {"type": "standard", "currency": "USD", "input_per_1m": 0.5, "output_per_1m": 10},
    "gemini-2.5-pro-preview-03-25": {"type": "standard", "currency": "USD", "input_per_1m": 0.5, "output_per_1m": 10},
    "gemini-2.5-pro-preview-05-06": {"type": "standard", "currency": "USD", "input_per_1m": 0.5, "output_per_1m": 10},
    "gemini-2.5-pro-preview-06-05": {"type": "standard", "currency": "USD", "input_per_1m": 0.5, "output_per_1m": 10},
    "gemini-2.5-pro-preview-tts": {"type": "standard", "currency": "USD", "input_per_1m": 0.5, "output_per_1m": 10},
    # Gemini 3 Pro Image Preview
    "gemini-3-pro-image-preview": {"type": "standard", "currency": "USD", "input_per_1m": 2, "output_per_1m": 12},
    # Gemini 3 Pro Preview：200K 阶梯 + 缓存储存 4.5
    "gemini-3-pro-preview": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 2}, {"up_to_k": None, "price_per_1m": 4}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 12}, {"up_to_k": None, "price_per_1m": 18}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.125}, {"up_to_k": None, "price_per_1m": 0.25}],
        "cache_storage_per_1m_per_hour": 4.5,
    },
    # Gemini 3 Pro Preview Thinking：同价
    "gemini-3-pro-preview-thinking": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 2}, {"up_to_k": None, "price_per_1m": 4}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 12}, {"up_to_k": None, "price_per_1m": 18}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.125}, {"up_to_k": None, "price_per_1m": 0.25}],
        "cache_storage_per_1m_per_hour": 4.5,
    },
    # Gemini 3 Flash Preview：标准定价
    "gemini-3-flash-preview": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.5, "output_per_1m": 3, "cache_read_per_1m": 0.05},
    # Gemini 2.5 Flash：标准定价
    "gemini-2.5-flash": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.3, "output_per_1m": 2.5, "cache_read_per_1m": 0.03},
    "gemini-2.5-flash-preview-09-2025": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.3, "output_per_1m": 2.5, "cache_read_per_1m": 0.03},
    # Gemini 2.5 Flash Image：与 Flash 相同定价（支持图片输入）
    "gemini-2.5-flash-image": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.3, "output_per_1m": 2.5, "cache_read_per_1m": 0.03},
    # Gemini 2.5 Pro：200K 阶梯 + 缓存储存 4.5
    "gemini-2.5-pro": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 1.25}, {"up_to_k": None, "price_per_1m": 2.5}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 10}, {"up_to_k": None, "price_per_1m": 15}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.125}, {"up_to_k": None, "price_per_1m": 0.25}],
        "cache_storage_per_1m_per_hour": 4.5,
    },
    # Gemini 2.5 Pro Exp 03-25：同 gemini-2.5-pro
    "gemini-2.5-pro-exp-03-25": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 1.25}, {"up_to_k": None, "price_per_1m": 2.5}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 10}, {"up_to_k": None, "price_per_1m": 15}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.125}, {"up_to_k": None, "price_per_1m": 0.25}],
        "cache_storage_per_1m_per_hour": 4.5,
    },
    # Gemini 2.5 Pro Thinking 1000：同 gemini-2.5-pro
    "gemini-2.5-pro-thinking-1000": {
        "type": "advanced",
        "currency": "USD",
        "input_tiers": [{"up_to_k": 200, "price_per_1m": 1.25}, {"up_to_k": None, "price_per_1m": 2.5}],
        "output_tiers": [{"up_to_k": 200, "price_per_1m": 10}, {"up_to_k": None, "price_per_1m": 15}],
        "cache_read_tiers": [{"up_to_k": 200, "price_per_1m": 0.125}, {"up_to_k": None, "price_per_1m": 0.25}],
        "cache_storage_per_1m_per_hour": 4.5,
    },
    # Grok 系列（美元）
    "grok-4-fast": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.2, "output_per_1m": 0.5, "cache_read_per_1m": 0.05},
    "grok-4-fast-non-reasoning": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.2, "output_per_1m": 0.5, "cache_read_per_1m": 0.05},
    "grok-4": {"type": "with_cache", "currency": "USD", "input_per_1m": 0.2, "output_per_1m": 0.5, "cache_read_per_1m": 0.05},
    # DeepSeek R1 系列（人民币）
    "deepseek-r1": {"type": "with_cache", "currency": "元", "input_per_1m": 4.0, "output_per_1m": 16.0, "cache_read_per_1m": 0.8},
    "deepseek-r1-32b": {"type": "with_cache", "currency": "元", "input_per_1m": 4.0, "output_per_1m": 16.0, "cache_read_per_1m": 0.8},
    # DeepSeek V3 系列（人民币）
    "deepseek-v3": {"type": "with_cache", "currency": "元", "input_per_1m": 2.0, "output_per_1m": 8.0, "cache_read_per_1m": 0.4},
    "deepseek-v3.1": {"type": "with_cache", "currency": "元", "input_per_1m": 4.0, "output_per_1m": 12.0, "cache_read_per_1m": 0.8},
    "deepseek-v3.1-thinking": {"type": "with_cache", "currency": "元", "input_per_1m": 4.0, "output_per_1m": 12.0, "cache_read_per_1m": 0.8},
    # DeepSeek V3.2：阶梯定价（人民币）
    "deepseek-v3.2": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [{"up_to_k": 32, "price_per_1m": 2}, {"up_to_k": 128, "price_per_1m": 4}, {"up_to_k": None, "price_per_1m": 4}],
        "output_tiers": [{"up_to_k": 32, "price_per_1m": 3}, {"up_to_k": 128, "price_per_1m": 6}, {"up_to_k": None, "price_per_1m": 6}],
        "cache_read_tiers": [{"up_to_k": 32, "price_per_1m": 0.4}, {"up_to_k": 128, "price_per_1m": 0.4}, {"up_to_k": None, "price_per_1m": 0.4}],
    },
    "deepseek-v3.2-thinking": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [{"up_to_k": 32, "price_per_1m": 2}, {"up_to_k": 128, "price_per_1m": 4}, {"up_to_k": None, "price_per_1m": 4}],
        "output_tiers": [{"up_to_k": 32, "price_per_1m": 3}, {"up_to_k": 128, "price_per_1m": 6}, {"up_to_k": None, "price_per_1m": 6}],
        "cache_read_tiers": [{"up_to_k": 32, "price_per_1m": 0.4}, {"up_to_k": 128, "price_per_1m": 0.4}, {"up_to_k": None, "price_per_1m": 0.4}],
    },
    # DeepSeek Chat / Reasoner（人民币）
    "deepseek-chat": {"type": "with_cache", "currency": "元", "input_per_1m": 2.0, "output_per_1m": 3.0, "cache_read_per_1m": 0.2},
    "deepseek-reasoner": {"type": "with_cache", "currency": "元", "input_per_1m": 2.0, "output_per_1m": 3.0, "cache_read_per_1m": 0.2},
    # 豆包系列（人民币）- 火山引擎
    # Doubao 1.5 Lite
    "doubao-1.5-lite-32k": {"type": "with_cache", "currency": "元", "input_per_1m": 0.3, "output_per_1m": 0.6, "cache_read_per_1m": 0.06, "cache_storage_per_1m_per_hour": 0.17},
    # Doubao 1.5 Pro
    "doubao-1.5-pro-256k": {"type": "with_cache", "currency": "元", "input_per_1m": 0.8, "output_per_1m": 2.0, "cache_read_per_1m": 0.16, "cache_storage_per_1m_per_hour": 0.17},
    "doubao-1.5-pro-32k": {"type": "with_cache", "currency": "元", "input_per_1m": 0.8, "output_per_1m": 2.0, "cache_read_per_1m": 0.16, "cache_storage_per_1m_per_hour": 0.17},
    # Doubao 1.5 Vision
    "doubao-1.5-thinking-vision-pro": {"type": "standard", "currency": "元", "input_per_1m": 3.0, "output_per_1m": 9.0},
    "doubao-1.5-vision-pro-32k": {"type": "standard", "currency": "元", "input_per_1m": 3.0, "output_per_1m": 9.0},
    # Doubao Lite
    "doubao-lite-4k": {"type": "with_cache", "currency": "元", "input_per_1m": 0.3, "output_per_1m": 0.6, "cache_read_per_1m": 0.06, "cache_storage_per_1m_per_hour": 0.17},
    # Doubao Seed 1.6：组合阶梯定价
    # 特殊逻辑：当 Input 在 (0, 32K] 时，Output 需要再细分
    # - Input (0, 32K] + Output (0, 0.2K]: input=0.8, output=2, cache_read=0.16
    # - Input (0, 32K] + Output (0.2K, ∞): input=0.8, output=8
    # 其他 Input 区间：Output 直接按 Input 区间定价
    # (32K, 128K]: input=1.2, output=16
    # (128K, 256K]: input=2.4, output=24
    # (256K, ∞): input=8, output=32
    "doubao-seed-1.6": {
        "type": "advanced",
        "currency": "元",
        # input_tiers 和 output_tiers 作为 fallback
        "input_tiers": [
            {"up_to_k": 32, "price_per_1m": 0.8},
            {"up_to_k": 128, "price_per_1m": 1.2},
            {"up_to_k": 256, "price_per_1m": 2.4},
            {"up_to_k": None, "price_per_1m": 8}
        ],
        "output_tiers": [
            {"up_to_k": 0.2, "price_per_1m": 2},
            {"up_to_k": None, "price_per_1m": 32}
        ],
        # 组合阶梯：优先匹配（每个档位 cache_read 都为 0.16）
        "combo_tiers": [
            # Input (0, 32K] + Output (0, 0.2K]
            {"input_up_to_k": 32, "output_up_to_k": 0.2, "input_price_per_1m": 0.8, "output_price_per_1m": 2, "cache_read_price_per_1m": 0.16},
            # Input (0, 32K] + Output (0.2K, ∞)
            {"input_up_to_k": 32, "output_up_to_k": None, "input_price_per_1m": 0.8, "output_price_per_1m": 8, "cache_read_price_per_1m": 0.16},
            # Input (32K, 128K] + 任意 Output
            {"input_up_to_k": 128, "output_up_to_k": None, "input_price_per_1m": 1.2, "output_price_per_1m": 16, "cache_read_price_per_1m": 0.16},
            # Input (128K, 256K] + 任意 Output
            {"input_up_to_k": 256, "output_up_to_k": None, "input_price_per_1m": 2.4, "output_price_per_1m": 24, "cache_read_price_per_1m": 0.16},
            # Input (256K, ∞) + 任意 Output
            {"input_up_to_k": None, "output_up_to_k": None, "input_price_per_1m": 8, "output_price_per_1m": 32, "cache_read_price_per_1m": 0.16},
        ],
        "cache_storage_per_1m_per_hour": 0.17,
    },
    # Doubao Seed 1.6 Flash：阶梯定价
    # (0, 32K] tokens: input=0.15, output=1.5, cache_read=0.03
    # (32K, 128K] tokens: input=0.3, output=3
    # (128K, 256K] tokens: input=0.6, output=6
    "doubao-seed-1.6-flash": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [
            {"up_to_k": 32, "price_per_1m": 0.15},
            {"up_to_k": 128, "price_per_1m": 0.3},
            {"up_to_k": 256, "price_per_1m": 0.6},
            {"up_to_k": None, "price_per_1m": 0.6}
        ],
        "output_tiers": [
            {"up_to_k": 32, "price_per_1m": 1.5},
            {"up_to_k": 128, "price_per_1m": 3},
            {"up_to_k": 256, "price_per_1m": 6},
            {"up_to_k": None, "price_per_1m": 6}
        ],
        "cache_read_tiers": [
            {"up_to_k": 32, "price_per_1m": 0.03},
            {"up_to_k": 128, "price_per_1m": 0.03},
            {"up_to_k": 256, "price_per_1m": 0.03},
            {"up_to_k": None, "price_per_1m": 0.03}
        ],
        "cache_storage_per_1m_per_hour": 0.17,
    },
    # Doubao Seed 1.6 Thinking：阶梯定价
    # (0, 32K] tokens: input=0.8, output=8, cache_read=0.16
    # (32K, 128K] tokens: input=1.2, output=16
    # (128K, 256K] tokens: input=2.4, output=24
    "doubao-seed-1.6-thinking": {
        "type": "advanced",
        "currency": "元",
        "input_tiers": [
            {"up_to_k": 32, "price_per_1m": 0.8},
            {"up_to_k": 128, "price_per_1m": 1.2},
            {"up_to_k": 256, "price_per_1m": 2.4},
            {"up_to_k": None, "price_per_1m": 2.4}
        ],
        "output_tiers": [
            {"up_to_k": 32, "price_per_1m": 8},
            {"up_to_k": 128, "price_per_1m": 16},
            {"up_to_k": 256, "price_per_1m": 24},
            {"up_to_k": None, "price_per_1m": 24}
        ],
        "cache_read_tiers": [
            {"up_to_k": 32, "price_per_1m": 0.16},
            {"up_to_k": 128, "price_per_1m": 0.16},
            {"up_to_k": 256, "price_per_1m": 0.16},
            {"up_to_k": None, "price_per_1m": 0.16}
        ],
        "cache_storage_per_1m_per_hour": 0.17,
    },

}


def get_builtin_price(model_name: str) -> dict | None:
    """按规范模型名查找内置定价，未配置返回 None。"""
    return BUILTIN_STANDARD_PRICES.get(model_name)
