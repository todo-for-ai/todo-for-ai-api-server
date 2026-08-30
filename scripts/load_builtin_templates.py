"""
加载内置角色模板到数据库

用法:
    python load_builtin_templates.py
    python load_builtin_templates.py --clear  # 清空后重新加载
"""

import json
import os
import sys
import argparse

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db, AgentRoleTemplate, AgentRoleTemplateStatus


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'data', 'agent_role_templates')


def load_template_from_file(filepath):
    """从 JSON 文件加载模板"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def template_exists(name):
    """检查模板是否已存在"""
    return AgentRoleTemplate.query.filter_by(
        name=name,
        is_builtin=True
    ).first() is not None


def create_or_update_template(data):
    """创建或更新模板"""
    existing = AgentRoleTemplate.query.filter_by(
        name=data['name'],
        is_builtin=True
    ).first()

    if existing:
        # 更新现有模板
        existing.display_name = data.get('display_name', existing.display_name)
        existing.description = data.get('description', existing.description)
        existing.avatar_url = data.get('avatar_url', existing.avatar_url)
        existing.category = data.get('category', existing.category)
        existing.capability_tags = data.get('capability_tags', existing.capability_tags)
        existing.system_prompt = data.get('system_prompt', existing.system_prompt)
        existing.soul_markdown = data.get('soul_markdown', existing.soul_markdown)
        existing.response_style = data.get('response_style', existing.response_style)
        existing.tool_policy = data.get('tool_policy', existing.tool_policy)
        existing.memory_policy = data.get('memory_policy', existing.memory_policy)
        existing.handoff_policy = data.get('handoff_policy', existing.handoff_policy)
        existing.llm_provider = data.get('llm_provider', existing.llm_provider)
        existing.llm_model = data.get('llm_model', existing.llm_model)
        existing.temperature = data.get('temperature', existing.temperature)
        existing.reasoning_mode = data.get('reasoning_mode', existing.reasoning_mode)
        existing.status = AgentRoleTemplateStatus.ACTIVE
        print(f"  Updated: {data['name']}")
        return existing
    else:
        # 创建新模板
        template = AgentRoleTemplate(
            workspace_id=None,  # 内置模板无 workspace
            created_by_user_id=1,  # 系统用户
            name=data['name'],
            display_name=data['display_name'],
            description=data.get('description'),
            avatar_url=data.get('avatar_url'),
            category=data.get('category', 'general'),
            capability_tags=data.get('capability_tags', []),
            system_prompt=data.get('system_prompt'),
            soul_markdown=data.get('soul_markdown'),
            response_style=data.get('response_style', {}),
            tool_policy=data.get('tool_policy', {}),
            memory_policy=data.get('memory_policy', {}),
            handoff_policy=data.get('handoff_policy', {}),
            llm_provider=data.get('llm_provider'),
            llm_model=data.get('llm_model'),
            temperature=data.get('temperature'),
            reasoning_mode=data.get('reasoning_mode', 'balanced'),
            is_builtin=True,
            status=AgentRoleTemplateStatus.ACTIVE,
            usage_count=0,
        )
        db.session.add(template)
        print(f"  Created: {data['name']}")
        return template


def clear_builtin_templates():
    """清空所有内置模板"""
    templates = AgentRoleTemplate.query.filter_by(is_builtin=True).all()
    for template in templates:
        db.session.delete(template)
    print(f"Cleared {len(templates)} builtin templates")


def load_all_templates(clear_first=False):
    """加载所有内置模板"""
    app = create_app()

    with app.app_context():
        if clear_first:
            clear_builtin_templates()
            db.session.commit()

        print(f"\nLoading templates from: {TEMPLATES_DIR}")

        if not os.path.exists(TEMPLATES_DIR):
            print(f"Error: Directory not found: {TEMPLATES_DIR}")
            return

        loaded = 0
        updated = 0

        for filename in os.listdir(TEMPLATES_DIR):
            if not filename.endswith('.json'):
                continue

            filepath = os.path.join(TEMPLATES_DIR, filename)
            print(f"\nProcessing: {filename}")

            try:
                data = load_template_from_file(filepath)
                existing = template_exists(data['name'])
                create_or_update_template(data)

                if existing:
                    updated += 1
                else:
                    loaded += 1

            except json.JSONDecodeError as e:
                print(f"  Error: Invalid JSON - {e}")
            except KeyError as e:
                print(f"  Error: Missing required field - {e}")
            except Exception as e:
                print(f"  Error: {e}")

        db.session.commit()

        print(f"\n{'='*50}")
        print(f"Summary:")
        print(f"  New templates: {loaded}")
        print(f"  Updated templates: {updated}")
        print(f"  Total: {loaded + updated}")
        print(f"{'='*50}\n")


def list_builtin_templates():
    """列出所有内置模板"""
    app = create_app()

    with app.app_context():
        templates = AgentRoleTemplate.query.filter_by(is_builtin=True).all()

        print(f"\n{'='*60}")
        print(f"Builtin Agent Role Templates ({len(templates)} total)")
        print(f"{'='*60}")

        for t in templates:
            status = "✓" if t.status == AgentRoleTemplateStatus.ACTIVE else "✗"
            print(f"{status} {t.name:20} | {t.display_name:25} | {t.category:15}")

        print(f"{'='*60}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Load builtin agent role templates')
    parser.add_argument('--clear', action='store_true',
                        help='Clear existing builtin templates before loading')
    parser.add_argument('--list', action='store_true',
                        help='List all builtin templates')

    args = parser.parse_args()

    if args.list:
        list_builtin_templates()
    else:
        load_all_templates(clear_first=args.clear)
