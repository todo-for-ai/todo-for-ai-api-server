"""
GitHub repo proxy — cache and forward GitHub API requests to avoid client-side rate limits.
"""

import os
import time

from flask import Blueprint

from .base import ApiResponse

github_proxy_bp = Blueprint('github_proxy', __name__)

# Simple in-memory cache: { key: (data, timestamp) }
_repo_cache: dict = {}
_CACHE_TTL = 15 * 60  # 15 minutes


@github_proxy_bp.route('/github/repo/<owner>/<repo>', methods=['GET'])
def proxy_github_repo(owner: str, repo: str):
    """Proxy GitHub repo info with server-side caching to avoid client rate limits."""
    cache_key = f"{owner}/{repo}"

    # Return cached data if fresh
    if cache_key in _repo_cache:
        data, ts = _repo_cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return ApiResponse.success(data=data).to_response()

    # Fetch from GitHub API
    import requests as http_requests

    github_token = os.environ.get('GITHUB_TOKEN')
    headers = {
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Todo-for-AI-Server',
    }
    if github_token:
        headers['Authorization'] = f'token {github_token}'

    try:
        resp = http_requests.get(
            f'https://api.github.com/repos/{owner}/{repo}',
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 403:
            # Rate limited — return stale cache if available
            if cache_key in _repo_cache:
                return ApiResponse.success(data=_repo_cache[cache_key][0]).to_response()
            return ApiResponse.error('GitHub API rate limit exceeded', 429).to_response()

        if resp.status_code != 200:
            return ApiResponse.error(f'GitHub API error: {resp.status_code}', resp.status_code).to_response()

        data = resp.json()
        filtered = {
            'name': data.get('name'),
            'full_name': data.get('full_name'),
            'description': data.get('description'),
            'html_url': data.get('html_url'),
            'stargazers_count': data.get('stargazers_count', 0),
            'forks_count': data.get('forks_count', 0),
            'language': data.get('language'),
            'updated_at': data.get('updated_at'),
        }

        # Cache the result
        _repo_cache[cache_key] = (filtered, time.time())

        return ApiResponse.success(data=filtered).to_response()

    except http_requests.RequestException as e:
        # Network error — return stale cache if available
        if cache_key in _repo_cache:
            return ApiResponse.success(data=_repo_cache[cache_key][0]).to_response()
        return ApiResponse.error(f'Failed to fetch GitHub data: {e}', 502).to_response()
