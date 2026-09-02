"""services/task_content.py — 任务协同文档模型单测（人与 Agent 协同写作）"""

import json

import pytest

from services.task_content import (
    AGENT_SECTION_MARK,
    append_agent_section,
    document_for_editing,
    iter_agent_sections,
    parse_task_document,
    render_task_document,
)


class TestParseTaskDocument:
    def test_empty_content(self):
        doc = parse_task_document(None)
        assert doc.source_format == 'empty'
        assert doc.body == ''
        assert doc.sections == []

        doc = parse_task_document('   ')
        assert doc.source_format == 'empty'

    def test_plain_markdown(self):
        doc = parse_task_document('# 标题\n\n正文内容')
        assert doc.source_format == 'markdown'
        assert doc.body == '# 标题\n\n正文内容'
        assert doc.sections == []

    def test_markdown_with_agent_sections(self):
        raw = (
            '# 人类需求\n\n实现登录页\n\n'
            '---\n\n'
            f'{AGENT_SECTION_MARK}（claude-code · 2026-09-02）\n\n'
            '已完成登录页，测试全绿。\n\n'
            '---\n\n'
            f'{AGENT_SECTION_MARK}（reviewer）\n\n'
            '评审通过。'
        )
        doc = parse_task_document(raw)
        assert doc.source_format == 'markdown'
        assert doc.body == '# 人类需求\n\n实现登录页'
        assert len(doc.sections) == 2
        assert doc.sections[0].label == 'claude-code · 2026-09-02'
        assert '测试全绿' in doc.sections[0].content
        assert doc.sections[1].label == 'reviewer'
        assert '评审通过' in doc.sections[1].content

    def test_legacy_json_envelope(self):
        raw = json.dumps({
            'content': '# 人类需求\n\n实现登录页',
            'agent_output': '已完成登录页',
            'agent_metadata': {'agent_name': 'openclaw'},
            'processed_by': 'agent',
        }, ensure_ascii=False)
        doc = parse_task_document(raw)
        assert doc.source_format == 'legacy_json'
        assert doc.body == '# 人类需求\n\n实现登录页'
        assert len(doc.sections) == 1
        assert doc.sections[0].content == '已完成登录页'
        assert 'openclaw' in doc.sections[0].label

    def test_legacy_json_without_agent_output(self):
        raw = json.dumps({'content': '# 只有正文', 'prompt': 'x'}, ensure_ascii=False)
        doc = parse_task_document(raw)
        assert doc.source_format == 'legacy_json'
        assert doc.body == '# 只有正文'
        assert doc.sections == []

    def test_json_array_is_not_envelope(self):
        raw = json.dumps([1, 2, 3])
        doc = parse_task_document(raw)
        assert doc.source_format == 'markdown'


class TestRenderAndRoundtrip:
    def test_render_includes_heading_and_separator(self):
        from services.task_content import AgentSection
        doc = parse_task_document('# 需求')
        doc.sections.append(AgentSection(label='agent-a', content='产出 A'))
        rendered = render_task_document(doc)
        assert rendered.startswith('# 需求')
        assert '---' in rendered
        assert f'{AGENT_SECTION_MARK}（agent-a）' in rendered
        assert '产出 A' in rendered

    def test_roundtrip_preserves_sections(self):
        raw = '# 需求\n\n实现登录页'
        raw2 = append_agent_section(raw, '产出 1', 'agent-a')
        raw3 = append_agent_section(raw2, '产出 2', 'agent-b')
        doc = parse_task_document(raw3)
        assert doc.body == '# 需求\n\n实现登录页'
        assert [s.label for s in doc.sections] == ['agent-a', 'agent-b']
        assert '产出 1' in doc.sections[0].content
        assert '产出 2' in doc.sections[1].content

    def test_legacy_envelope_normalized_on_append(self):
        legacy = json.dumps({
            'content': '# 需求',
            'agent_output': '旧产出',
            'agent_metadata': {'agent_name': 'openclaw'},
        }, ensure_ascii=False)
        new_raw = append_agent_section(legacy, '新产出', 'claude-code')
        doc = parse_task_document(new_raw)
        assert doc.source_format == 'markdown'
        assert doc.body == '# 需求'
        assert [s.label for s in doc.sections] == ['openclaw', 'claude-code']
        # 归一化后不再是 JSON 串
        assert not new_raw.lstrip().startswith('{')

    def test_human_edit_keeps_existing_sections(self):
        raw = append_agent_section('# 需求', '产出 1', 'agent-a')
        # 模拟人类在编辑器里改写正文（分节保留）
        doc = parse_task_document(raw)
        doc.body = '# 需求（已修订）'
        human_saved = render_task_document(doc)
        doc2 = parse_task_document(human_saved)
        assert doc2.body == '# 需求（已修订）'
        assert len(doc2.sections) == 1

    def test_section_without_body(self):
        new_raw = append_agent_section('', '只有产出', 'agent-a')
        doc = parse_task_document(new_raw)
        assert doc.body == ''
        assert len(doc.sections) == 1


class TestEditingAndDisplay:
    def test_document_for_editing_normalizes_legacy(self):
        legacy = json.dumps({
            'content': '# 需求',
            'agent_output': '旧产出',
            'agent_metadata': {},
        }, ensure_ascii=False)
        editable = document_for_editing(legacy)
        assert '# 需求' in editable
        assert AGENT_SECTION_MARK in editable
        assert '旧产出' in editable
        assert not editable.lstrip().startswith('{')

    def test_document_for_editing_empty(self):
        assert document_for_editing(None) == ''
        assert document_for_editing('') == ''

    def test_iter_agent_sections(self):
        raw = append_agent_section('# 需求', '产出', 'agent-a')
        sections = iter_agent_sections(raw)
        assert len(sections) == 1
        assert sections[0]['label'] == 'agent-a'
        assert sections[0]['content'] == '产出'
        assert AGENT_SECTION_MARK in sections[0]['heading']
