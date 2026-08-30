#!/usr/bin/env python3
"""
通知投递 Worker

消费 Redis 队列中的 notification_deliveries 任务，并负责失败重试。
"""

import sys
import time

from app import create_app
from core.notification_dispatcher import dispatch_once, dispatch_batch


def main():
    app = create_app()
    with app.app_context():
        if '--once' in sys.argv:
            result = dispatch_batch(max_items=100)
            print(f"[notification-dispatcher] batch={result}")
            return

        while True:
            result = dispatch_once(timeout_seconds=5)
            print(f"[notification-dispatcher] {result}")
            time.sleep(1 if result.get('status') == 'idle' else 0.2)


if __name__ == '__main__':
    main()
