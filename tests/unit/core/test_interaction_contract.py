"""多 Agent 交互契约校验（core/interaction_contract.py）单元回归。

覆盖：请求/回执两个归一化器的全部校验分支（字段缺失、类型错误、
长度上限、枚举值、自指目标、SLA 边界、敏感级、置信度区间、
resolver 一致性）与归一化输出的完整形状。
"""

import pytest

from core.interaction_contract import (
    normalize_interaction_request_payload,
    normalize_interaction_resolve_payload,
)


def _valid_request(**overrides):
    payload = {
        'task_id': 12,
        'attempt_id': 'att-1',
        'interaction_type': 'handoff_task',
        'target_agent_id': 9,
        'contract': {
            'intent': '完成代码评审',
            'input_schema': {'a': 1},
            'output_schema': {},
            'sla_seconds': 600,
        },
        'chain_context': {'chain_id': 3, 'role': 'reviewer', 'stage': '2'},
        'security_context': {
            'grant_id': 'g-1',
            'required_capabilities': ['code_review', 'code_review', ' '],
            'sensitivity_level': 'HIGH',
        },
        'metadata': {'k': 'v'},
    }
    payload.update(overrides)
    return payload


class TestRequestPayload:
    def test_happy_path_full_shape(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(), current_agent_id=5)
        assert err is None
        assert data['task_id'] == 12
        assert data['source_agent_id'] == 5
        assert data['target_agent_id'] == 9
        assert data['contract']['sla_seconds'] == 600
        assert data['chain_context'] == {'chain_id': 3, 'role': 'reviewer',
                                         'stage': '2'}
        # capability 去重 + 空白过滤；敏感级转小写
        assert data['security_context']['required_capabilities'] == ['code_review']
        assert data['security_context']['sensitivity_level'] == 'high'
        assert data['metadata'] == {'k': 'v'}

    def test_non_dict_payload(self):
        assert normalize_interaction_request_payload([1], current_agent_id=5) == \
            (None, 'request payload must be object')

    @pytest.mark.parametrize("bad", [None, '', 'abc', 0, -3])
    def test_task_id_must_be_positive(self, bad):
        data, err = normalize_interaction_request_payload(
            _valid_request(task_id=bad), current_agent_id=5)
        assert data is None
        assert err == 'task_id must be positive integer'

    def test_attempt_id_too_long(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(attempt_id='x' * 65), current_agent_id=5)
        assert (data, err) == (None, 'attempt_id exceeds max length 64')

    def test_interaction_type_restricted(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(interaction_type='party'), current_agent_id=5)
        assert data is None
        assert 'interaction_type must be one of' in err

    def test_target_agent_must_be_positive(self):
        _, err = normalize_interaction_request_payload(
            _valid_request(target_agent_id=0), current_agent_id=5)
        assert err == 'target_agent_id must be positive integer'

    def test_chain_stage_too_long(self):
        _, err = normalize_interaction_request_payload(
            _valid_request(chain_context={'stage': 's' * 65}),
            current_agent_id=5)
        assert err == 'chain_context.stage exceeds max length 64'

    def test_target_same_as_source_rejected(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(target_agent_id=5), current_agent_id=5)
        assert err == 'target_agent_id must be different from source agent'

    def test_contract_must_be_object(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(contract=[]), current_agent_id=5)
        assert err == 'contract must be object'

    def test_intent_required(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(contract={'intent': '  '}), current_agent_id=5)
        assert err == 'contract.intent is required'

    def test_intent_too_long(self):
        data, err = normalize_interaction_request_payload(
            _valid_request(contract={'intent': 'x' * 513}), current_agent_id=5)
        assert err == 'contract.intent exceeds max length 512'

    @pytest.mark.parametrize("field", ['input_schema', 'output_schema'])
    def test_schemas_must_be_objects(self, field):
        data, err = normalize_interaction_request_payload(
            _valid_request(contract={'intent': 'i', field: []}),
            current_agent_id=5)
        assert err == f'contract.{field} must be object'

    def test_sla_defaults_and_bound(self):
        data, _ = normalize_interaction_request_payload(
            _valid_request(contract={'intent': 'i'}), current_agent_id=5)
        assert data['contract']['sla_seconds'] == 300

        data, err = normalize_interaction_request_payload(
            _valid_request(contract={'intent': 'i', 'sla_seconds': 86401}),
            current_agent_id=5)
        assert err == 'contract.sla_seconds must be <= 86400'

        data, _ = normalize_interaction_request_payload(
            _valid_request(contract={'intent': 'i', 'sla_seconds': '45'}),
            current_agent_id=5)
        assert data['contract']['sla_seconds'] == 45

    def test_chain_context_validations(self):
        _, err = normalize_interaction_request_payload(
            _valid_request(chain_context='nope'), current_agent_id=5)
        assert err == 'chain_context must be object'

        _, err = normalize_interaction_request_payload(
            _valid_request(chain_context={'role': 'x' * 65}), current_agent_id=5)
        assert err == 'chain_context.role exceeds max length 64'

    def test_security_context_validations(self):
        _, err = normalize_interaction_request_payload(
            _valid_request(security_context='nope'), current_agent_id=5)
        assert err == 'security_context must be object'

        _, err = normalize_interaction_request_payload(
            _valid_request(security_context={'grant_id': 'g' * 65}),
            current_agent_id=5)
        assert err == 'security_context.grant_id exceeds max length 64'

        _, err = normalize_interaction_request_payload(
            _valid_request(security_context={'required_capabilities': 'nope'}),
            current_agent_id=5)
        assert err == 'security_context.required_capabilities must be array'

        _, err = normalize_interaction_request_payload(
            _valid_request(security_context={
                'required_capabilities': ['x' * 129]}), current_agent_id=5)
        assert 'item exceeds max length 128' in err

        _, err = normalize_interaction_request_payload(
            _valid_request(security_context={'sensitivity_level': 'secret'}),
            current_agent_id=5)
        assert 'sensitivity_level must be one of' in err

    def test_metadata_must_be_object(self):
        _, err = normalize_interaction_request_payload(
            _valid_request(metadata=[1]), current_agent_id=5)
        assert err == 'metadata must be object'


def _valid_resolve(**overrides):
    payload = {
        'task_id': 12,
        'attempt_id': 'att-1',
        'status': 'succeeded',
        'result': {'message': 'done', 'confidence': 0.9},
    }
    payload.update(overrides)
    return payload


class TestResolvePayload:
    def test_happy_path(self):
        data, err = normalize_interaction_resolve_payload(
            _valid_resolve(), current_agent_id=5, interaction_id='ix-1')
        assert err is None
        assert data['interaction_id'] == 'ix-1'
        assert data['status'] == 'succeeded'
        assert data['result']['confidence'] == 0.9
        assert data['result']['resolver_agent_id'] == 5

    def test_non_dict_payload(self):
        assert normalize_interaction_resolve_payload(
            'x', current_agent_id=5, interaction_id='i') == \
            (None, 'request payload must be object')

    def test_interaction_id_required(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(), current_agent_id=5, interaction_id='   ')
        assert err == 'interaction_id is required'

    def test_task_id_and_attempt_id_validations(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(task_id='abc'), current_agent_id=5,
            interaction_id='i')
        assert err == 'task_id must be positive integer'

        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(attempt_id='a' * 65), current_agent_id=5,
            interaction_id='i')
        assert err == 'attempt_id exceeds max length 64'

    def test_status_restricted(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(status='pending'), current_agent_id=5,
            interaction_id='i')
        assert 'status must be one of' in err

    def test_result_must_be_object(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(result=[1]), current_agent_id=5, interaction_id='i')
        assert err == 'result must be object'

    @pytest.mark.parametrize("status", ['failed', 'blocked', 'timeout'])
    def test_failure_statuses_require_error_code(self, status):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(status=status, result={}), current_agent_id=5,
            interaction_id='i')
        assert err == f'result.error_code is required when status={status}'

    def test_error_code_too_long(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(status='failed',
                           result={'error_code': 'e' * 65}),
            current_agent_id=5, interaction_id='i')
        assert err == 'result.error_code exceeds max length 64'

    def test_message_too_long(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(result={'message': 'm' * 1025}),
            current_agent_id=5, interaction_id='i')
        assert err == 'result.message exceeds max length 1024'

    def test_confidence_validations(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(result={'confidence': 'high'}),
            current_agent_id=5, interaction_id='i')
        assert err == 'result.confidence must be numeric'

        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(result={'confidence': 1.5}),
            current_agent_id=5, interaction_id='i')
        assert err == 'result.confidence must be between 0 and 1'

    def test_output_payload_must_be_object(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(result={'output_payload': [1]}),
            current_agent_id=5, interaction_id='i')
        assert err == 'result.output_payload must be object'

    def test_resolver_must_be_current_agent(self):
        _, err = normalize_interaction_resolve_payload(
            _valid_resolve(result={'resolver_agent_id': 77}),
            current_agent_id=5, interaction_id='i')
        assert err == 'result.resolver_agent_id must equal current agent'
