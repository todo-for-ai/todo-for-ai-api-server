"""
Agent Runtime LLM 指标摄取 API

daemon 每次引擎调用上报一条（幂等键 call_id）；归属用户由服务端解析。
"""

from flask import Blueprint, g

from models import db
from .base import ApiResponse, validate_json_request
from .agent_common import agent_session_required
from services.llm_metrics import record_calls

agent_runtime_llm_metrics_bp = Blueprint('agent_runtime_llm_metrics', __name__)


@agent_runtime_llm_metrics_bp.route('/agent/llm-metrics/batch', methods=['POST'])
@agent_session_required
def ingest_llm_metrics_batch():
    """批量摄取 LLM 引擎调用指标（单批上限 100 条，重复 call_id 跳过）。"""
    data = validate_json_request(required_fields=['calls'])
    if isinstance(data, tuple):
        return data

    try:
        inserted, skipped = record_calls(g.current_agent, data.get('calls'))
    except ValueError as e:
        return ApiResponse.error(str(e), 400).to_response()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return ApiResponse.error(f'Failed to ingest LLM metrics: {e}', 500).to_response()

    return ApiResponse.success(
        {'inserted': inserted, 'skipped': skipped},
        'LLM metrics ingested',
    ).to_response()
