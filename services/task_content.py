"""任务协同文档模型（人与 Agent 协同写作）

task.content 是一篇人与 Agent 共同书写的 Markdown 文档：
- 人类正文在前，Agent 产出以带归属的分节追加在后；
- 分节标题是普通 Markdown 标题（`## 🤖 Agent 产出（名字 · 时间）`），
  在任何渲染器（含 Milkdown WYSIWYG）中都可见、可继续编辑，人类保存不会破坏归属信息；
- 历史遗留的 JSON 信封（{"content": ..., "agent_output": ...}）在读取时
  无损转换为正文 + 分节，下次写回时归一化为纯 Markdown。
"""

import json
import re
from dataclasses import dataclass, field

# 分节标题前缀（前后端共用同一标记，前端实现见 webpage src/utils/taskContent.ts）
AGENT_SECTION_MARK = '## 🤖 Agent 产出'

_HEADING_RE = re.compile(
    r'^' + re.escape(AGENT_SECTION_MARK) + r'[（(](.*?)[)）]\s*$',
    re.MULTILINE,
)


@dataclass
class AgentSection:
    """一篇文档中的一段 Agent 产出"""

    label: str  # 归属标签，如 "claude-code · 2026-09-02 10:30"
    content: str  # 分节正文（Markdown）
    heading: str = ''  # 完整标题行

    def __post_init__(self):
        if not self.heading:
            self.heading = f'{AGENT_SECTION_MARK}（{self.label}）'

    def to_dict(self):
        return {'label': self.label, 'content': self.content, 'heading': self.heading}


@dataclass
class TaskDocument:
    """解析后的任务协同文档"""

    body: str = ''
    sections: list = field(default_factory=list)  # list[AgentSection]
    # markdown：纯 Markdown 文档；legacy_json：历史 JSON 信封；empty：空内容
    source_format: str = 'markdown'

    def to_dict(self):
        return {
            'body': self.body,
            'sections': [s.to_dict() for s in self.sections],
            'source_format': self.source_format,
        }


def _label_from_metadata(metadata):
    """从历史 JSON 信封的 agent_metadata 里尽量还原归属标签"""
    parts = []
    for key in ('agent_name', 'agent', 'processed_by'):
        value = metadata.get(key) if isinstance(metadata, dict) else None
        if value:
            parts.append(str(value))
    return ' · '.join(parts) if parts else 'agent'


def _strip_trailing_hr(text):
    """剥掉分节切片边界上的 '---' 分隔线（render 在每个分节前插入）"""
    text = text.strip()
    while text.endswith('---'):
        text = text[:-3].strip()
    return text


def parse_task_document(raw):
    """把 task.content 原始字符串解析为 TaskDocument。

    兼容三种形态：空内容、纯 Markdown（含 Agent 分节）、历史 JSON 信封。
    """
    if not raw or not str(raw).strip():
        return TaskDocument(source_format='empty')

    raw = str(raw)

    # 历史 JSON 信封：commit 曾把 Agent 产出以 JSON blob 写回
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and ('agent_output' in parsed or 'content' in parsed):
        body = parsed.get('content')
        body = body if isinstance(body, str) else ('' if body is None else str(body))
        sections = []
        output = parsed.get('agent_output')
        if output:
            metadata = parsed.get('agent_metadata') or {}
            sections.append(AgentSection(
                label=_label_from_metadata(metadata) or parsed.get('processed_by', 'agent'),
                content=str(output),
            ))
        return TaskDocument(body=body, sections=sections, source_format='legacy_json')

    # 纯 Markdown：按分节标题切分
    matches = list(_HEADING_RE.finditer(raw))
    if not matches:
        return TaskDocument(body=raw, source_format='markdown')

    body = _strip_trailing_hr(raw[:matches[0].start()])
    sections = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        content = _strip_trailing_hr(raw[match.end():end])
        sections.append(AgentSection(
            label=match.group(1).strip(),
            content=content,
            heading=match.group(0).strip(),
        ))
    return TaskDocument(body=body, sections=sections, source_format='markdown')


def render_task_document(doc):
    """TaskDocument → 完整 Markdown 文档字符串"""
    parts = [doc.body.rstrip()] if doc.body and doc.body.strip() else []
    for section in doc.sections:
        parts.append(f"---\n\n{section.heading}\n\n{section.content.strip()}")
    return '\n\n'.join(parts)


def append_agent_section(raw, output, agent_label, agent_metadata=None):
    """把一次 Agent 产出追加为文档分节，返回新的 task.content 字符串。

    - raw 为历史 JSON 信封时顺便归一化为纯 Markdown；
    - 已有分节保持不动（多次提交按顺序累积，人类此前的编辑不丢失）。
    """
    doc = parse_task_document(raw)
    label = str(agent_label or '').strip() or _label_from_metadata(agent_metadata) or 'agent'
    doc.sections.append(AgentSection(label=label, content=str(output or '')))
    return render_task_document(doc)


def document_for_editing(raw):
    """编辑器加载用：任何形态都归一化为纯 Markdown 文档（正文 + Agent 分节）。

    人类在编辑器里看到并继续书写 Agent 的贡献，而不是看到 JSON 串。
    """
    doc = parse_task_document(raw)
    if doc.source_format == 'empty':
        return ''
    return render_task_document(doc)


def iter_agent_sections(raw):
    """展示用：列出文档中的 Agent 分节（归属标签 + 内容）"""
    return [s.to_dict() for s in parse_task_document(raw).sections]
