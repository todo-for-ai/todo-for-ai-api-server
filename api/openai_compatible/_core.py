"""蓝图、OpenAI 认证装饰器与常量配置（原样搬移）。"""

from functools import wraps
from flask import Blueprint, request, g

from api.base import ApiResponse
from models import AgentSession

openai_bp = Blueprint('openai_compatible', __name__)


def openai_auth_required(f):
    """
    OpenAI API 认证装饰器 - 支持多种认证方式：
    1. API Token
    2. JWT
    3. Agent Session Token
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return ApiResponse.unauthorized('Authentication required').to_response()

        token = auth_header.split(' ')[1]

        # 1. 尝试 API Token 认证
        from models import ApiToken
        api_token = ApiToken.verify_token(token)
        if api_token:
            g.current_user = api_token.user
            g.current_token = api_token
            g.auth_method = 'api_token'
            return f(*args, **kwargs)

        # 2. 尝试 Agent Session 认证
        session = AgentSession.verify_session_token(token)
        if session:
            from models import Agent
            agent = Agent.query.get(session.agent_id)
            if agent and agent.status and agent.status.value == 'active':
                g.current_agent = agent
                g.current_agent_session = session
                g.auth_method = 'agent_session'
                return f(*args, **kwargs)

        # 3. 尝试 JWT 认证
        try:
            from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
            verify_jwt_in_request()
            try:
                user_id = int(get_jwt_identity())
            except (TypeError, ValueError):
                user_id = None
            if user_id:
                from models import User
                user = User.query.get(user_id)
                if user and user.is_active():
                    g.current_user = user
                    g.auth_method = 'jwt'
                    return f(*args, **kwargs)
        except Exception:
            pass

        return ApiResponse.unauthorized('Authentication required').to_response()

    return decorated_function


# ============== 常量配置 ==============

# 缓存配置
CACHE_TTL_SECONDS = 300  # 5分钟缓存
CACHE_KEY_PREFIX = "openai:"
CACHE_CONSISTENCY_LOCK_PREFIX = "lock:openai:"
CACHE_INVALIDATION_CHANNEL = "openai:cache:invalidate"

# 请求限制
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10MB
MAX_MESSAGES_LENGTH = 100  # 最大消息数
MAX_PROMPT_LENGTH = 100000  # 最大prompt长度

# 支持的模型列表
SUPPORTED_MODELS = [
    {"id": "gpt-4", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-4-turbo", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-3.5-turbo", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-3.5-turbo-16k", "object": "model", "created": 1677610602, "owned_by": "openai"},
]
