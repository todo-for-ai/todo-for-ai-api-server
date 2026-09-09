#!/usr/bin/env python3
"""
Agent Cron 调度器（独立进程形态）

核心 tick 逻辑在 core/agent_cron_scheduler.py（与 app 内守护线程共享同一实
现）；本脚本是独立进程薄壳，供不便于在 app 内开线程的部署形态使用（pm2/
supervisor 托管）。本地开发推荐直接 AGENT_CRON_SCHEDULER_ENABLED=true。
"""

import sys
import time

from app import create_app
from core.agent_cron_scheduler import tick


def main():
    app = create_app()
    with app.app_context():
        while True:
            fired, matched = tick()
            print(f"[agent-cron] matched={matched} fired={fired}")
            if '--once' in sys.argv:
                break
            time.sleep(30)


if __name__ == '__main__':
    main()
