"""AI 请求审计：缓冲 + 定时批量落库。"""

import threading
import time
from threading import Thread
from typing import List

from services.ai.errors import AIRequestContext


class AIAuditLogger:
    """AI 审计日志记录器"""

    def __init__(self):
        self._buffer: List[AIRequestContext] = []
        self._lock = threading.Lock()
        self._flush_interval = 10  # 每10秒刷新
        self._start_flush_timer()

    def _start_flush_timer(self):
        """启动定时刷新"""
        def flush_periodically():
            while True:
                time.sleep(self._flush_interval)
                self.flush()

        thread = Thread(target=flush_periodically, daemon=True)
        thread.start()

    def log(self, context: AIRequestContext):
        """记录请求上下文"""
        with self._lock:
            self._buffer.append(context)

        # 如果缓冲区太大，立即刷新
        if len(self._buffer) >= 100:
            self.flush()

    def flush(self):
        """刷新日志到数据库"""
        if not self._buffer:
            return

        with self._lock:
            logs_to_save = self._buffer.copy()
            self._buffer.clear()

        try:
            # 异步保存到数据库
            from models import db, AIRequestLog

            for ctx in logs_to_save:
                log_entry = AIRequestLog(
                    request_id=ctx.request_id,
                    user_id=ctx.user_id,
                    user_email=ctx.user_email,
                    feature=ctx.feature,
                    prompt_tokens=ctx.prompt_tokens,
                    completion_tokens=ctx.completion_tokens,
                    total_tokens=ctx.total_tokens,
                    latency_ms=ctx.latency_ms,
                    cache_hit=ctx.cache_hit,
                    error_code=ctx.error_code.value,
                    error_message=ctx.error_message,
                    created_at=ctx.created_at
                )
                db.session.add(log_entry)

            db.session.commit()
        except Exception as e:
            # 保存失败时，打印错误但不抛出
            print(f"[AI Audit] Failed to save logs: {e}")
            # 重新放入缓冲区
            with self._lock:
                self._buffer.extend(logs_to_save)
