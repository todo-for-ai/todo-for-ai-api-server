"""
LLM 调用工具模块
用于统一调用系统配置的大模型 API
"""

import requests
import json
import os


def get_llm_config():
    """获取系统 LLM 配置"""
    try:
        from models import SystemSettings
        # 尝试在 Flask 上下文中获取
        from flask import current_app
        if current_app:
            return SystemSettings.get_llm_config()
    except:
        pass

    # 从环境变量获取配置
    return {
        'provider': os.environ.get('LLM_PROVIDER', 'openai'),
        'api_base': os.environ.get('LLM_API_BASE', 'https://api.openai.com/v1'),
        'api_key': os.environ.get('LLM_API_KEY', ''),
        'model': os.environ.get('LLM_MODEL', 'gpt-4'),
    }


def call_llm(messages, model=None, temperature=0.7, max_tokens=2000, stream=False):
    """
    调用 LLM API

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名称，默认使用配置中的模型
        temperature: 采样温度
        max_tokens: 最大 token 数
        stream: 是否流式返回

    Returns:
        dict: 包含 success, content, error 的字典
    """
    config = get_llm_config()

    api_base = config.get('api_base', 'https://api.openai.com/v1')
    api_key = config.get('api_key', '')
    model = model or config.get('model', 'gpt-4')

    if not api_key:
        return {'success': False, 'error': 'LLM API key not configured'}

    try:
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        payload = {
            'model': model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'stream': stream
        }

        response = requests.post(
            f'{api_base}/chat/completions',
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 200:
            result = response.json()
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            return {
                'success': True,
                'content': content,
                'usage': result.get('usage', {})
            }
        else:
            return {
                'success': False,
                'error': f'API returned status {response.status_code}: {response.text}'
            }

    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'LLM API timeout'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'LLM API connection error'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def call_llm_json(messages, model=None, temperature=0.7, max_tokens=2000):
    """
    调用 LLM 并解析返回为 JSON

    Args:
        messages: 消息列表
        model: 模型名称
        temperature: 采样温度
        max_tokens: 最大 token 数

    Returns:
        dict: 包含 success, data, error 的字典
    """
    result = call_llm(messages, model, temperature, max_tokens)

    if not result['success']:
        return result

    try:
        # 尝试从内容中提取 JSON
        content = result['content']

        # 如果内容被 markdown 代码块包裹，提取内部
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0]
        elif '```' in content:
            content = content.split('```')[1].split('```')[0]

        content = content.strip()
        data = json.loads(content)
        return {'success': True, 'data': data}
    except json.JSONDecodeError as e:
        return {
            'success': False,
            'error': f'Failed to parse JSON: {str(e)}',
            'raw_content': result['content']
        }
