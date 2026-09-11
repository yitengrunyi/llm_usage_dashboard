"""模型名称标准化工具。

目标: 同一底层模型, 不管走的什么渠道/带什么 context size/带什么日期 tag,
统一到同一个 key, 这样跨渠道的用量才能正确聚合。

削掉的 (不影响模型身份):
- vendor 前缀:  anthropic. / openai. / google. / bedrock. / azure. (供应商路由前缀)
- 日期 tag:     -20251101 / -2024-07-18 / -05-20 (snapshot 版本)
- context size: -200k / -272k / -1m  (同模型不同窗口, 计费维度合并)
- 实验标签:     -preview / -exp / -beta / -ga (生命周期标签)

保留的 (是不同模型, 价格/能力都不同):
- 功能变体: -mini / -nano / -lite / -flash / -pro / -opus / -sonnet / -haiku
- 模态变体: -image / -vision / -audio / -thinking / -reasoning
- 主版本号: 4 / 4o / 4.1 / 5 / 5.4

补全:
- gpt5 → gpt-5  (有些渠道把横线吞了)
"""
import re


_VENDOR_PREFIXES = ("anthropic.", "openai.", "google.", "bedrock.", "azure.", "vertex.")


def normalize_model_name(name: str) -> str:
    if not name:
        return name
    s = name.strip()

    # vendor 路由前缀
    low = s.lower()
    for p in _VENDOR_PREFIXES:
        if low.startswith(p):
            s = s[len(p):]
            break

    # gpt5 → gpt-5 (在 gpt/claude/gemini 后紧跟数字时补横线)
    s = re.sub(r'^(gpt|claude|gemini)(\d)', r'\1-\2', s, flags=re.IGNORECASE)

    # 日期: YYYYMMDD / YYYY-MM-DD / -MM-DD 在末尾
    s = re.sub(r'-20\d{6}', '', s)
    s = re.sub(r'-20\d{2}-\d{2}-\d{2}', '', s)
    s = re.sub(r'-\d{2}-\d{2}$', '', s)

    # context size: -200k / -272k / -1m / -128k 等, 可能在末尾或夹在 -preview 之间
    s = re.sub(r'-\d+[km]\b', '', s, flags=re.IGNORECASE)

    # ucloud 套餐标签: -unlimit 是无限套餐变体, 跟 base model 是同一个
    s = re.sub(r'-unlimit\b', '', s, flags=re.IGNORECASE)

    # 生命周期标签 (preview/exp/beta/ga), 可能出现在末尾或中段
    s = re.sub(r'-(preview|exp|experimental|beta|alpha|ga)\b', '', s, flags=re.IGNORECASE)

    # 清掉多余横线
    s = re.sub(r'-+', '-', s).strip('-')

    return s
