"""project_repo 包（由单文件 api/project_repo.py 原样拆分）。

兼容导出：蓝图、公开路由函数与 _upsert_pr_evidence（api/github_app.py 引用）。
GitHubClient 相关符号从 _shared 导出；单测 patch 路径为
`api.project_repo._shared.GitHubClient`。
"""

from ._shared import (  # noqa: F401
    project_repo_bp,
    GitHubClient,
    GitHubClientError,
    resolve_token,
    _upsert_pr_evidence,
    _get_binding,
    _ensure_task_access,
)
from .binding import (  # noqa: F401
    get_project_repo,
    bind_project_repo,
    unbind_project_repo,
)
from .pull_requests import (  # noqa: F401
    create_task_pull_request,
    get_task_pull_request,
)
from .lifecycle import (  # noqa: F401
    approve_task_pull_request,
    list_pending_pr_approvals,
    review_task_pull_request,
    merge_task_pull_request,
)
