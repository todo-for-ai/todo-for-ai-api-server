#!/usr/bin/env python3
"""
简版 Agent Cron 调度器

扫描到期 cron 触发器，生成 agent_runs 记录（queued）。
用于平台托管 Runner 的触发入口。
"""

import hashlib
import time
from datetime import datetime

from app import create_app
from models import db, AgentTrigger, AgentTriggerType, AgentRun, AgentRunState
from api.agent_automation import _compute_next_fire_at
from api.agent_common import generate_id


def _idempotency_key(trigger_id, fire_at):
    raw = f"cron:{trigger_id}:{fire_at.isoformat()}"
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return f"cron:{trigger_id}:{digest[:40]}"


def tick(limit=200):
    now = datetime.utcnow()
    triggers = AgentTrigger.query.filter(
        AgentTrigger.trigger_type == AgentTriggerType.CRON.value,
        AgentTrigger.enabled.is_(True),
        AgentTrigger.next_fire_at.isnot(None),
        AgentTrigger.next_fire_at <= now,
    ).order_by(AgentTrigger.next_fire_at.asc()).limit(limit).all()

    created = 0
    for trigger in triggers:
        fire_at = trigger.next_fire_at or now
        idem_key = _idempotency_key(trigger.id, fire_at)
        if AgentRun.query.filter_by(idempotency_key=idem_key).first():
            trigger.last_triggered_at = now
            trigger.next_fire_at = _compute_next_fire_at(trigger.cron_expr or '', now)
            continue

        run = AgentRun(
            run_id=generate_id('run'),
            workspace_id=trigger.workspace_id,
            agent_id=trigger.agent_id,
            trigger_id=trigger.id,
            trigger_reason='cron.tick',
            input_payload={
                'trigger_type': 'cron',
                'cron_expr': trigger.cron_expr,
                'fire_at': fire_at.isoformat(),
            },
            state=AgentRunState.QUEUED.value,
            scheduled_at=now,
            attempt_count=0,
            idempotency_key=idem_key,
            created_by='system:cron_scheduler',
        )
        db.session.add(run)
        created += 1

        trigger.last_triggered_at = now
        trigger.next_fire_at = _compute_next_fire_at(trigger.cron_expr or '', now)

    db.session.commit()
    return created, len(triggers)


def main():
    app = create_app()
    with app.app_context():
        while True:
            created, matched = tick()
            print(f"[agent-cron] matched={matched} created={created}")
            if '--once' in __import__('sys').argv:
                break
            time.sleep(30)


if __name__ == '__main__':
    main()
