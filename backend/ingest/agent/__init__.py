"""自愈 Agent — 巡检 12 家供应商入库状态, LLM 诊断 + 安全自愈.

模块:
- signals  确定性信号收集 (零 LLM 成本, 干净直接退出)
- llm      OpenAI 兼容 chat client + 工具调用循环
- prompts  中文系统提示词 (内嵌排障知识库)
- tools    读工具 + 动作工具 + 代码级护栏
- report   报告落库 + 飞书卡片 (含降级卡片)
- patrol   编排: 锁 → 信号 → LLM/降级 → 验证 → 报告
- router   /api/ingest/agent/* HTTP 端点
"""
