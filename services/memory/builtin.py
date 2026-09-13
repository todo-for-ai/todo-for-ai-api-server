"""builtin 记忆后端：基于平台自有 AgentExperience + KnowledgeEntry 的召回。

零新依赖：关键词/标签/领域匹配（SQL 层），置信度加权排序。
检索是词面匹配而非语义匹配——语义召回由 mem0 后端（可选）提供，
接口一致，切换不改调用方。
"""

from models import AgentExperience, KnowledgeEntry

from . import MemoryHit


def _keywords(query, limit=8):
    """极简分词：去停用词取长度 ≥2 的词元；CJK 串无空格可分，追加二元切分。"""
    stopwords = {'的', '了', '和', '与', '及', '或', '在', '对', '把', '是',
                 'the', 'a', 'an', 'and', 'or', 'of', 'to', 'for', 'in', 'on',
                 'with', 'by', 'is', 'are'}

    def _is_cjk(s):
        return any('\u4e00' <= ch <= '\u9fff' for ch in s)

    tokens = []
    for raw in str(query or '').replace('，', ' ').replace('。', ' ').split():
        token = raw.strip('()[]{}:：;；,，.。!！?？\'"').lower()
        if len(token) < 2 or token in stopwords:
            continue
        tokens.append(token)
        if _is_cjk(token) and len(token) > 2:
            seen = set()
            for i in range(len(token) - 1):
                gram = token[i:i + 2]
                if gram not in seen and gram not in stopwords:
                    seen.add(gram)
                    tokens.append(gram)
        if len(tokens) >= limit:
            break
    return tokens[:limit]


def _snippet(text, keywords, limit=200):
    """截取含关键词的片段（无命中取头部）。"""
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    lowered = text.lower()
    for kw in keywords:
        idx = lowered.find(kw)
        if idx >= 0:
            start = max(0, idx - 40)
            return ('…' if start > 0 else '') + text[start:start + limit] + '…'
    return text[:limit] + '…'


class BuiltinMemoryBackend:
    """自有经验的 SQL 召回（experience + knowledge 两源）。"""

    def recall(self, query, workspace_id=None, agent_id=None, top_k=3):
        keywords = _keywords(query)
        hits = []
        for kw in keywords:
            hits.extend(self._recall_experiences(kw, agent_id))
            hits.extend(self._recall_knowledge(kw, workspace_id))
            if len(hits) >= top_k * 4:
                break
        if not hits:
            return []
        # 命中多关键词的去重加权 + 置信度排序
        merged = {}
        for hit in hits:
            key = (hit.kind, hit.source_id)
            if key in merged:
                merged[key].score += hit.score
            else:
                merged[key] = hit
        ranked = sorted(merged.values(), key=lambda h: h.score, reverse=True)
        return ranked[:top_k]

    def _recall_experiences(self, keyword, agent_id, per_keyword=3):
        query = AgentExperience.query.filter(
            AgentExperience.is_valid.is_(True),
            AgentExperience.outcome_pattern.ilike(f'%{keyword}%')
            | AgentExperience.key_learnings.ilike(f'%{keyword}%')
            | AgentExperience.domain.ilike(f'%{keyword}%'),
        )
        if agent_id is not None:
            query = query.filter(
                (AgentExperience.agent_id == int(agent_id))
                | (AgentExperience.is_shared.is_(True))
            )
        rows = query.order_by(
            (AgentExperience.confidence * AgentExperience.times_reused).desc(),
            AgentExperience.id.desc(),
        ).limit(per_keyword).all()
        return [
            MemoryHit(
                kind='experience',
                title=f"{row.experience_type or '经验'}"
                      + (f"@{row.domain}" if row.domain else ''),
                snippet=_snippet(row.key_learnings or row.outcome_pattern, [keyword]),
                score=0.6 * float(row.confidence or 0.5),
                source_id=row.id,
            )
            for row in rows
        ]

    def _recall_knowledge(self, keyword, workspace_id, per_keyword=3):
        query = KnowledgeEntry.query.filter(
            KnowledgeEntry.is_valid.is_(True),
            KnowledgeEntry.title.ilike(f'%{keyword}%')
            | KnowledgeEntry.content.ilike(f'%{keyword}%'),
        )
        rows = query.order_by(
            KnowledgeEntry.confidence.desc(), KnowledgeEntry.id.desc(),
        ).limit(per_keyword).all()
        return [
            MemoryHit(
                kind='knowledge',
                title=row.title[:120],
                snippet=_snippet(row.content, [keyword]),
                score=0.5 * float(row.confidence or 0.5),
                source_id=row.id,
            )
            for row in rows
        ]

    def remember(self, **kwargs):
        """builtin 后端的写入由业务侧直接落 AgentExperience/KnowledgeEntry，
        不经此接口（写入治理——版本/审计/置信度——已在模型层实现）。"""
        raise NotImplementedError('builtin write goes through domain models directly')
