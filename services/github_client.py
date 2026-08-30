"""
GitHub REST API 轻量客户端（P1.1 代码平面）

平台侧对 GitHub 的写操作集合：建分支、开 PR、查 PR、合并 PR。
只做与自主迭代闭环相关的最小面，认证用仓库绑定 token 或部署级 GITHUB_TOKEN。
"""

import os
from typing import Optional, Tuple

import requests

from services.github_app import decrypt_str

GITHUB_API_BASE = 'https://api.github.com'
DEFAULT_TIMEOUT = 15


class GitHubClientError(Exception):
    """GitHub API 调用失败（带状态码，便于端点透传）。"""

    def __init__(self, message: str, status_code: int = 500, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


class GitHubClient:
    """最小 GitHub REST 客户端（token 认证）。"""

    def __init__(self, token: Optional[str]):
        self.token = token

    def _headers(self):
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'Todo-for-AI-Server',
        }
        if self.token:
            headers['Authorization'] = f'token {self.token}'
        return headers

    def _request(self, method: str, path: str, payload=None, params=None) -> Tuple[int, dict]:
        url = path if path.startswith('http') else f'{GITHUB_API_BASE}{path}'
        try:
            resp = requests.request(
                method, url, json=payload, params=params,
                headers=self._headers(), timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            raise GitHubClientError(f'GitHub request failed: {e}', 502)

        if resp.status_code in (204,):
            return resp.status_code, {}
        try:
            data = resp.json()
        except ValueError:
            data = {'raw': resp.text[:500]}

        if resp.status_code >= 400:
            message = ''
            if isinstance(data, dict):
                message = data.get('message') or ''
            raise GitHubClientError(
                f'GitHub API error {resp.status_code}: {message}',
                resp.status_code,
                details=data if isinstance(data, dict) else {},
            )
        return resp.status_code, data

    # ── 仓库 / 分支 ──

    def get_repo(self, owner: str, repo: str) -> dict:
        _, data = self._request('GET', f'/repos/{owner}/{repo}')
        return data

    def get_branch(self, owner: str, repo: str, branch: str) -> Optional[dict]:
        try:
            _, data = self._request('GET', f'/repos/{owner}/{repo}/branches/{branch}')
            return data
        except GitHubClientError as e:
            if e.status_code == 404:
                return None
            raise

    def create_branch(self, owner: str, repo: str, branch: str, from_branch: str) -> dict:
        """从 from_branch 的 HEAD 创建新分支。"""
        _, ref = self._request('GET', f'/repos/{owner}/{repo}/git/ref/heads/{from_branch}')
        sha = ref.get('object', {}).get('sha')
        if not sha:
            raise GitHubClientError(f'Base branch {from_branch!r} has no HEAD sha', 422)
        status, data = self._request('POST', f'/repos/{owner}/{repo}/git/refs', payload={
            'ref': f'refs/heads/{branch}',
            'sha': sha,
        })
        return data

    def ensure_branch(self, owner: str, repo: str, branch: str, from_branch: str) -> bool:
        """确保分支存在；已存在则不动，返回是否新创建。"""
        if self.get_branch(owner, repo, branch) is not None:
            return False
        self.create_branch(owner, repo, branch, from_branch)
        return True

    # ── Pull Request ──

    def create_pull_request(self, owner: str, repo: str, head: str, base: str,
                            title: str, body: str = '', draft: bool = False) -> dict:
        status, data = self._request('POST', f'/repos/{owner}/{repo}/pulls', payload={
            'title': title,
            'head': head,
            'base': base,
            'body': body,
            'draft': draft,
        })
        return data

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        _, data = self._request('GET', f'/repos/{owner}/{repo}/pulls/{number}')
        return data

    def list_pull_requests(self, owner: str, repo: str, head: Optional[str] = None,
                           base: Optional[str] = None, state: str = 'open') -> list:
        params = {'state': state}
        if head:
            params['head'] = f'{owner}:{head}'
        if base:
            params['base'] = base
        _, data = self._request('GET', f'/repos/{owner}/{repo}/pulls', params=params)
        return data

    def merge_pull_request(self, owner: str, repo: str, number: int, merge_method: str = 'merge') -> dict:
        status, data = self._request(
            'PUT', f'/repos/{owner}/{repo}/pulls/{number}/merge',
            payload={'merge_method': merge_method},
        )
        return data


def resolve_token(binding) -> Optional[str]:
    """解析仓库访问 token：绑定级（加密存储）优先，回退部署级 GITHUB_TOKEN。"""
    if binding is not None and binding.token_encrypted:
        decrypted = decrypt_str(binding.token_encrypted)
        if decrypted:
            return decrypted
        # 解密失败（密钥轮换/缺失）时回退环境 token，避免整个闭环不可用
    return os.environ.get('GITHUB_TOKEN') or None
