"""AI 基础设施包（从 ai_service.py 拆分）。

- config.py: 容错配置（DB 读取 + 缓存）
- errors.py: 错误码与请求上下文
- rate_limiter.py: 滑动窗口限流
- response_cache.py: 内存 + Redis 双级缓存
- audit_logger.py: 请求审计落库
- llm_service.py: LLM 调用编排（限流→缓存→请求→错误映射）

对外入口保持 services.ai_service 门面不变。
"""
