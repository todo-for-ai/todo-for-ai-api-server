"""外部系统连接器包（从 connectors.py 拆分）。

外部事件 → 平台任务/评论的导入：
- Linear / GitLab / Jira webhook（Issue/Comment 的 create|update）→ 平台任务
  upsert（external key 记在 tasks.creator_identifier = '<provider>:<key>'）
  与追加式评论（TaskLog），状态按各平台状态模型映射。
- 验签：Linear=HMAC-SHA256(raw body, secret)，GitLab/Jira=令牌常量时间比较，
  全部 fail-closed。
- 每次处理写 TaskEventOutbox（connector.<provider>.*），出站可见于开放事件流，
  与读侧协议构成双向同步闭环。

配置（ExternalConnectorConfig）：workspace + provider 一条，secret 加密存储。

本 __init__ 兼容旧导入路径：from services.connectors import ingest_jira, ...
"""

from services.connectors.store import (  # noqa: F401
    get_connector,
    list_connectors,
    upsert_connector,
)
from services.connectors.verify import (  # noqa: F401
    verify_gitlab_token,
    verify_jira_token,
    verify_linear_signature,
)
from services.connectors.jira import (  # noqa: F401
    JIRA_STATUS_MAP,
    ingest_jira,
)
from services.connectors.gitlab import (  # noqa: F401
    GITLAB_STATE_MAP,
    ingest_gitlab,
)
from services.connectors.linear import (  # noqa: F401
    LINEAR_STATE_MAP,
    ingest_linear,
)
