#!/usr/bin/env python3
"""私有化部署离线自检脚本（Phase 4 企业能力）

在部署机上直接运行（读取 .env / 环境变量，连库做迁移完整性检查）：

    python scripts/deploy_check.py            # JSON 报告，存在 error 时退出码 1
    python scripts/deploy_check.py --quiet    # 仅输出 ok 与 errors

用于部署前/升级后的离线自检，与 GET /system/deploy/check 端点共用同一检查面。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXIT_OK = 0
EXIT_FAILED = 1


def main() -> int:
    quiet = '--quiet' in sys.argv

    from app import create_app
    from services.deploy_check import run_deploy_checks

    app = create_app()
    with app.app_context():
        report = run_deploy_checks()

    if quiet:
        print('ok' if report['ok'] else 'failed')
        for name in report['errors']:
            print('ERROR:', name)
    else:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    return EXIT_OK if report['ok'] else EXIT_FAILED


if __name__ == '__main__':
    sys.exit(main())
