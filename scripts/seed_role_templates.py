#!/usr/bin/env python3
"""
内置岗位角色种子脚本：把 role_taxonomy 数据集展开并幂等写入
agent_role_templates（is_builtin=True，workspace_id=NULL，industry 标注）。

用法:
    python scripts/seed_role_templates.py                 # 写入（默认 creator=user id 1）
    python scripts/seed_role_templates.py --dry-run       # 只统计不落库
    python scripts/seed_role_templates.py --creator-user-id 1
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from models import db, AgentRoleTemplate, AgentRoleTemplateStatus, User  # noqa: E402
from scripts.role_taxonomy import build_expanded, build_system_prompt  # noqa: E402


def chunked(seq, size=500):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--creator-user-id", type=int, default=1)
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        creator = db.session.get(User, args.creator_user_id)
        if creator is None:
            print(f"❌ creator user id={args.creator_user_id} 不存在，请用 --creator-user-id 指定")
            sys.exit(1)

        rows = build_expanded()
        by_name = {r["name"]: r for r in rows}
        print(f"数据集：{len(rows)} 个工种")

        existing = {
            t.name: t
            for t in AgentRoleTemplate.query.filter(
                AgentRoleTemplate.name.in_(list(by_name.keys()))
            ).all()
        }
        print(f"库中已存在：{len(existing)} 个")

        to_insert = [r for r in rows if r["name"] not in existing]
        if args.dry_run:
            print(f"[dry-run] 将新增 {len(to_insert)}，更新 {len(existing)}")
            return

        created = 0
        for batch in chunked(to_insert, 500):
            templates = [
                AgentRoleTemplate(
                    workspace_id=None,
                    created_by_user_id=creator.id,
                    name=r["name"],
                    display_name=r["display_name"],
                    description=r["description"],
                    category=r["category"],
                    industry=r["industry"],
                    capability_tags=r["capability_tags"],
                    system_prompt=build_system_prompt(
                        r["display_name"], r["industry"], r["skills"], r["category"]
                    ),
                    is_builtin=True,
                    status=AgentRoleTemplateStatus.ACTIVE,
                )
                for r in batch
            ]
            db.session.bulk_save_objects(templates)
            db.session.commit()
            created += len(batch)
        print(f"✅ 新增 {created} 个内置岗位模板")

        updated = 0
        for name, r in by_name.items():
            t = existing.get(name)
            if t is None:
                continue
            t.industry = r["industry"]
            t.category = r["category"]
            t.display_name = r["display_name"]
            t.description = r["description"]
            t.capability_tags = r["capability_tags"]
            updated += 1
        db.session.commit()
        print(f"✅ 更新 {updated} 个既有模板的行业/技能信息")


if __name__ == "__main__":
    main()
