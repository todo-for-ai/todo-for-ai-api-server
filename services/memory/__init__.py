"""Agent 记忆层（可插拔）：统一 remember / recall 接口。

平台已有的经验/知识基建（AgentExperience / KnowledgeEntry）覆盖了
结构化记忆的写入与治理，缺的是「检索进 prompt 的自动注入」。本包把
召回抽象成后端无关的接口：

- builtin（默认）：零新依赖，基于 AgentExperience + KnowledgeEntry 的
  关键词/标签/领域匹配召回（置信度加权）；
- mem0（可选）：mem0ai 适配器（Apache-2.0），向量语义召回——向量后端
  可复用平台现有 Redis（mem0 原生支持 redis vector set），无需引入
  新数据库；未安装 mem0ai 包时工厂自动回退 builtin。

调用方只依赖 `recall_for_query()` / `get_memory_backend()`，后端切换
不改任何业务代码。任何记忆异常都不允许阻断业务主链路（派发/循环推进）。
"""

import logging
import os

log = logging.getLogger(__name__)

DEFAULT_TOP_K = 3
DEFAULT_MEMORY_SECTION_CHARS = 800


def _env_int(name, default, lo, hi):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def recall_top_k() -> int:
    return _env_int('AGENT_MEMORY_TOP_K', DEFAULT_TOP_K, 0, 10)


def memory_section_chars() -> int:
    return _env_int('AGENT_MEMORY_SECTION_CHARS', DEFAULT_MEMORY_SECTION_CHARS, 100, 5000)


class MemoryHit:
    """一条被召回的记忆（后端无关的统一形状）。"""

    __slots__ = ('kind', 'title', 'snippet', 'score', 'source_id')

    def __init__(self, kind, title, snippet, score=0.0, source_id=None):
        self.kind = kind            # experience | knowledge | fact(外部后端)
        self.title = (title or '').strip()
        self.snippet = (snippet or '').strip()
        self.score = float(score or 0.0)
        self.source_id = source_id

    def to_line(self) -> str:
        label = {'experience': '经验', 'knowledge': '知识', 'fact': '事实'}.get(self.kind, self.kind)
        line = f"- [{label}] {self.title}" if self.title else f"- [{label}] {self.snippet[:80]}"
        if self.snippet and self.title:
            line += f"：{self.snippet}"
        return line


def get_memory_backend():
    """按 AGENT_MEMORY_BACKEND 选择记忆后端；mem0 不可用时回退 builtin。"""
    from .builtin import BuiltinMemoryBackend

    choice = (os.environ.get('AGENT_MEMORY_BACKEND') or 'builtin').strip().lower()
    if choice == 'mem0':
        try:
            from .mem0_backend import Mem0MemoryBackend

            return Mem0MemoryBackend()
        except Exception as e:  # noqa: BLE001 - 未安装 mem0ai/配置缺失 → 降级
            log.warning("memory.mem0_unavailable_fallback_builtin: %s", e)
    return BuiltinMemoryBackend()


def recall_for_query(query, workspace_id=None, agent_id=None, top_k=None):
    """便捷入口：召回与 query 相关的记忆块（格式化后的行列表）。

    任何异常返回空列表——记忆增强是锦上添花，绝不阻断业务主链路。
    """
    if not (query or '').strip():
        return []
    try:
        backend = get_memory_backend()
        top_k = top_k if top_k is not None else recall_top_k()
        if top_k <= 0:
            return []
        hits = backend.recall(query, workspace_id=workspace_id,
                              agent_id=agent_id, top_k=top_k)
        lines = [h.to_line() for h in hits if h and (h.title or h.snippet)]
        limit = memory_section_chars()
        out, used = [], 0
        for line in lines:
            if used + len(line) > limit:
                break
            out.append(line)
            used += len(line)
        return out
    except Exception:  # noqa: BLE001
        log.warning("memory.recall_failed", exc_info=True)  # exc_info 为 stdlib 合法参数
        return []
