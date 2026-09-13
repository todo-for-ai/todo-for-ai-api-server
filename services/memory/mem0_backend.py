"""mem0 适配器（可选后端）：把 mem0ai 映射到统一记忆接口。

仅当 `pip install mem0ai` 且配置了 embedding/LLM 后才可用；工厂在
import 失败时自动回退 builtin（services/memory/__init__.py）。向量后端
推荐复用平台现有 Redis（mem0 原生支持 redis vector set），无需新数据库。

本适配器刻意保持薄：写入沿用 mem0 的 fact 抽取管线，读取只做 add/search
两件事的映射；过滤（user_id/run_id）按 workspace/agent 维度隔离。
"""

import logging
import os

from . import MemoryHit

log = logging.getLogger(__name__)


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'mem0 backend requires env {name}')
    return value


class Mem0MemoryBackend:
    """mem0ai Memory 的适配（惰性初始化，首次调用才连后端）。"""

    def __init__(self):
        try:
            from mem0 import Memory  # noqa: F401 - 探测依赖，缺失时工厂回退
        except ImportError as e:
            raise RuntimeError(f'mem0ai not installed: {e}') from e
        self._memory = None

    def _client(self):
        if self._memory is None:
            from mem0 import Memory

            config = {
                'vector_store': {
                    'provider': os.environ.get('AGENT_MEMORY_VECTOR_PROVIDER', 'redis'),
                    'config': {
                        'redis_url': os.environ.get('REDIS_URL', 'redis://localhost:6379/2'),
                        'collection_name': os.environ.get('AGENT_MEMORY_COLLECTION', 'todo4ai_memory'),
                    },
                },
            }
            self._memory = Memory.from_config(config)
        return self._memory

    def remember(self, text, *, workspace_id=None, agent_id=None, metadata=None):
        payload = {'messages': [{'role': 'user', 'content': text}]}
        if workspace_id is not None:
            payload['user_id'] = f'ws:{int(workspace_id)}'
        if agent_id is not None:
            payload['run_id'] = f'agent:{int(agent_id)}'
        if metadata:
            payload['metadata'] = metadata
        return self._client().add(**payload)

    def recall(self, query, workspace_id=None, agent_id=None, top_k=3):
        filters = {}
        if workspace_id is not None:
            filters['user_id'] = f'ws:{int(workspace_id)}'
        results = self._client().search(query=query, limit=int(top_k), **filters)
        rows = results.get('results') if isinstance(results, dict) else results
        hits = []
        for row in rows or []:
            text = row.get('memory') or row.get('text') or ''
            if not text:
                continue
            hits.append(MemoryHit(
                kind='fact',
                title='mem0',
                snippet=text[:300],
                score=float(row.get('score') or 0.0),
                source_id=row.get('id'),
            ))
        return hits
