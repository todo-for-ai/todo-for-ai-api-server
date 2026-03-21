"""
Canonical validation for multi-agent interaction contract payloads.
"""

from typing import Any, Dict, List, Optional, Tuple


INTERACTION_TYPES = {
    'handoff_task',
    'request_capability',
    'proxy_execute',
    'critique_feedback',
}

INTERACTION_STATUSES = {
    'succeeded',
    'failed',
    'blocked',
    'cancelled',
    'timeout',
}

SENSITIVITY_LEVELS = {
    'low',
    'medium',
    'high',
    'critical',
}

FAILURE_REQUIRED_STATUSES = {'failed', 'blocked', 'timeout'}
ERROR_CODE_MAX_LENGTH = 64


def _parse_positive_int(raw_value: Any) -> Optional[int]:
    if raw_value in (None, ''):
        return None
    try:
        value = int(str(raw_value).strip())
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def _normalize_text(raw_value: Any, max_length: int, field_name: str, required: bool = False) -> Tuple[Optional[str], Optional[str]]:
    text = str(raw_value or '').strip()
    if not text:
        if required:
            return None, f'{field_name} is required'
        return None, None
    if len(text) > max_length:
        return None, f'{field_name} exceeds max length {max_length}'
    return text, None


def _normalize_schema(raw_value: Any, field_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if raw_value is None:
        return {}, None
    if not isinstance(raw_value, dict):
        return None, f'{field_name} must be object'
    return raw_value, None


def _normalize_capabilities(raw_value: Any) -> Tuple[List[str], Optional[str]]:
    if raw_value in (None, ''):
        return [], None
    if not isinstance(raw_value, list):
        return [], 'security_context.required_capabilities must be array'

    normalized: List[str] = []
    seen = set()
    for item in raw_value:
        text = str(item or '').strip()
        if not text:
            continue
        if len(text) > 128:
            return [], 'security_context.required_capabilities item exceeds max length 128'
        if text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized, None


def normalize_interaction_request_payload(
    payload: Dict[str, Any],
    *,
    current_agent_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(payload, dict):
        return None, 'request payload must be object'

    task_id = _parse_positive_int(payload.get('task_id'))
    if task_id is None:
        return None, 'task_id must be positive integer'

    attempt_id, err = _normalize_text(payload.get('attempt_id'), 64, 'attempt_id', required=False)
    if err:
        return None, err

    interaction_type = str(payload.get('interaction_type') or '').strip().lower()
    if interaction_type not in INTERACTION_TYPES:
        return None, f'interaction_type must be one of: {", ".join(sorted(INTERACTION_TYPES))}'

    target_agent_id = _parse_positive_int(payload.get('target_agent_id'))
    if target_agent_id is None:
        return None, 'target_agent_id must be positive integer'

    if target_agent_id == int(current_agent_id):
        return None, 'target_agent_id must be different from source agent'

    contract = payload.get('contract')
    if not isinstance(contract, dict):
        return None, 'contract must be object'

    intent, err = _normalize_text(contract.get('intent'), 512, 'contract.intent', required=True)
    if err:
        return None, err

    input_schema, err = _normalize_schema(contract.get('input_schema'), 'contract.input_schema')
    if err:
        return None, err
    output_schema, err = _normalize_schema(contract.get('output_schema'), 'contract.output_schema')
    if err:
        return None, err

    sla_seconds = _parse_positive_int(contract.get('sla_seconds'))
    if sla_seconds is None:
        sla_seconds = 300
    if sla_seconds > 86400:
        return None, 'contract.sla_seconds must be <= 86400'

    chain_context_raw = payload.get('chain_context') or {}
    if chain_context_raw and not isinstance(chain_context_raw, dict):
        return None, 'chain_context must be object'
    chain_context_raw = chain_context_raw if isinstance(chain_context_raw, dict) else {}

    chain_id = _parse_positive_int(chain_context_raw.get('chain_id'))
    chain_role, err = _normalize_text(chain_context_raw.get('role'), 64, 'chain_context.role')
    if err:
        return None, err
    chain_stage, err = _normalize_text(chain_context_raw.get('stage'), 64, 'chain_context.stage')
    if err:
        return None, err

    security_context_raw = payload.get('security_context') or {}
    if security_context_raw and not isinstance(security_context_raw, dict):
        return None, 'security_context must be object'
    security_context_raw = security_context_raw if isinstance(security_context_raw, dict) else {}

    grant_id, err = _normalize_text(security_context_raw.get('grant_id'), 64, 'security_context.grant_id')
    if err:
        return None, err
    required_capabilities, err = _normalize_capabilities(security_context_raw.get('required_capabilities'))
    if err:
        return None, err
    sensitivity_level = str(security_context_raw.get('sensitivity_level') or 'medium').strip().lower()
    if sensitivity_level not in SENSITIVITY_LEVELS:
        return None, f'security_context.sensitivity_level must be one of: {", ".join(sorted(SENSITIVITY_LEVELS))}'

    metadata = payload.get('metadata') or {}
    if metadata and not isinstance(metadata, dict):
        return None, 'metadata must be object'
    metadata = metadata if isinstance(metadata, dict) else {}

    return {
        'task_id': task_id,
        'attempt_id': attempt_id,
        'interaction_type': interaction_type,
        'source_agent_id': int(current_agent_id),
        'target_agent_id': target_agent_id,
        'chain_context': {
            'chain_id': chain_id,
            'role': chain_role,
            'stage': chain_stage,
        },
        'contract': {
            'intent': intent,
            'input_schema': input_schema or {},
            'output_schema': output_schema or {},
            'sla_seconds': sla_seconds,
        },
        'security_context': {
            'grant_id': grant_id,
            'required_capabilities': required_capabilities,
            'sensitivity_level': sensitivity_level,
        },
        'metadata': metadata,
    }, None


def normalize_interaction_resolve_payload(
    payload: Dict[str, Any],
    *,
    current_agent_id: int,
    interaction_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(payload, dict):
        return None, 'request payload must be object'

    interaction_id_text, err = _normalize_text(interaction_id, 64, 'interaction_id', required=True)
    if err:
        return None, err

    task_id = _parse_positive_int(payload.get('task_id'))
    if task_id is None:
        return None, 'task_id must be positive integer'

    attempt_id, err = _normalize_text(payload.get('attempt_id'), 64, 'attempt_id')
    if err:
        return None, err

    status = str(payload.get('status') or '').strip().lower()
    if status not in INTERACTION_STATUSES:
        return None, f'status must be one of: {", ".join(sorted(INTERACTION_STATUSES))}'

    result = payload.get('result') or {}
    if result and not isinstance(result, dict):
        return None, 'result must be object'
    result = result if isinstance(result, dict) else {}

    error_code, err = _normalize_text(result.get('error_code'), ERROR_CODE_MAX_LENGTH, 'result.error_code')
    if err:
        return None, err
    if status in FAILURE_REQUIRED_STATUSES and not error_code:
        return None, f'result.error_code is required when status={status}'

    message, err = _normalize_text(result.get('message'), 1024, 'result.message')
    if err:
        return None, err

    confidence_raw = result.get('confidence')
    confidence = None
    if confidence_raw is not None:
        try:
            confidence = float(confidence_raw)
        except Exception:
            return None, 'result.confidence must be numeric'
        if confidence < 0 or confidence > 1:
            return None, 'result.confidence must be between 0 and 1'

    output_payload = result.get('output_payload') or {}
    if output_payload and not isinstance(output_payload, dict):
        return None, 'result.output_payload must be object'
    output_payload = output_payload if isinstance(output_payload, dict) else {}

    resolver_agent_id = _parse_positive_int(result.get('resolver_agent_id'))
    if resolver_agent_id is not None and resolver_agent_id != int(current_agent_id):
        return None, 'result.resolver_agent_id must equal current agent'

    return {
        'interaction_id': interaction_id_text,
        'task_id': task_id,
        'attempt_id': attempt_id,
        'status': status,
        'result': {
            'error_code': error_code,
            'message': message,
            'confidence': confidence,
            'output_payload': output_payload,
            'resolver_agent_id': int(current_agent_id),
        },
    }, None

