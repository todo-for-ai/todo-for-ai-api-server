#!/usr/bin/env python3
"""
OpenAI 兼容 API 功能测试脚本

测试内容：
1. GET /v1/models - 获取模型列表
2. GET /v1/models/{id} - 获取单个模型
3. POST /v1/chat/completions - 聊天补全（非流式）
4. POST /v1/chat/completions - 聊天补全（流式）
5. POST /v1/embeddings - 文本嵌入
"""

import requests
import json
import sys

# 配置
BASE_URL = "http://127.0.0.1:50110"
API_TOKEN = "ags_wAZauDtoDGc64aEetrrDEpCx3kipA-tpwFYYyDCZFpY"

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}

def test_models_list():
    """测试获取模型列表"""
    print("\n" + "="*60)
    print("测试 1: GET /v1/models - 获取模型列表")
    print("="*60)

    url = f"{BASE_URL}/v1/models"
    response = requests.get(url, headers=headers)

    print(f"状态码: {response.status_code}")
    print(f"响应头: {dict(response.headers)}")

    if response.status_code == 200:
        data = response.json()
        print(f"\n响应体:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        # 验证 OpenAI 标准格式
        if "object" in data and data["object"] == "list":
            print("\n✅ 格式正确: object='list'")
        else:
            print("\n❌ 格式错误: 缺少 object='list'")

        if "data" in data and isinstance(data["data"], list):
            print(f"✅ 模型数量: {len(data['data'])}")
            if len(data["data"]) > 0:
                model = data["data"][0]
                required_fields = ["id", "object", "created", "owned_by"]
                for field in required_fields:
                    if field in model:
                        print(f"✅ 模型包含字段: {field}")
                    else:
                        print(f"❌ 模型缺少字段: {field}")
        return True
    else:
        print(f"\n❌ 请求失败: {response.text}")
        return False

def test_model_get():
    """测试获取单个模型"""
    print("\n" + "="*60)
    print("测试 2: GET /v1/models/gpt-4 - 获取单个模型")
    print("="*60)

    url = f"{BASE_URL}/v1/models/gpt-4"
    response = requests.get(url, headers=headers)

    print(f"状态码: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"\n响应体:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        if "id" in data and data["id"] == "gpt-4":
            print("\n✅ 模型 ID 正确")
        return True
    else:
        print(f"\n❌ 请求失败: {response.text}")
        return False

def test_chat_completions():
    """测试聊天补全（非流式）"""
    print("\n" + "="*60)
    print("测试 3: POST /v1/chat/completions - 聊天补全（非流式）")
    print("="*60)

    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "你是一个有帮助的助手。"},
            {"role": "user", "content": "你好，请用中文回复"}
        ],
        "temperature": 0.7,
        "max_tokens": 100,
        "stream": False
    }

    response = requests.post(url, headers=headers, json=payload)

    print(f"状态码: {response.status_code}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")

    if response.status_code == 200:
        data = response.json()
        print(f"\n响应体:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        # 验证 OpenAI 标准格式
        checks = [
            ("id" in data, "包含 id"),
            ("object" in data and data["object"] == "chat.completion", "object='chat.completion'"),
            ("created" in data, "包含 created"),
            ("model" in data, "包含 model"),
            ("choices" in data and isinstance(data["choices"], list), "包含 choices 数组"),
            ("usage" in data, "包含 usage"),
        ]

        for check, desc in checks:
            if check:
                print(f"✅ {desc}")
            else:
                print(f"❌ {desc}")

        if "choices" in data and len(data["choices"]) > 0:
            choice = data["choices"][0]
            choice_checks = [
                ("index" in choice, "choice 包含 index"),
                ("message" in choice, "choice 包含 message"),
                ("finish_reason" in choice, "choice 包含 finish_reason"),
            ]
            for check, desc in choice_checks:
                if check:
                    print(f"✅ {desc}")
                else:
                    print(f"❌ {desc}")

        return True
    else:
        print(f"\n❌ 请求失败: {response.text}")
        return False

def test_chat_completions_stream():
    """测试聊天补全（流式）"""
    print("\n" + "="*60)
    print("测试 4: POST /v1/chat/completions - 聊天补全（流式）")
    print("="*60)

    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "你好"}
        ],
        "stream": True
    }

    response = requests.post(url, headers=headers, json=payload, stream=True)

    print(f"状态码: {response.status_code}")
    print(f"Content-Type: {response.headers.get('content-type', 'N/A')}")

    if response.status_code == 200:
        print("\n流式响应内容（前5行）:")
        line_count = 0
        for line in response.iter_lines():
            if line:
                line_str = line.decode('utf-8')
                print(f"  {line_str}")
                line_count += 1
                if line_count >= 5:
                    print("  ...")
                    break
        print("✅ 流式响应正常")
        return True
    else:
        print(f"\n❌ 请求失败: {response.text}")
        return False

def test_embeddings():
    """测试文本嵌入"""
    print("\n" + "="*60)
    print("测试 5: POST /v1/embeddings - 文本嵌入")
    print("="*60)

    url = f"{BASE_URL}/v1/embeddings"
    payload = {
        "model": "text-embedding-ada-002",
        "input": "这是一段测试文本"
    }

    response = requests.post(url, headers=headers, json=payload)

    print(f"状态码: {response.status_code}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")

    if response.status_code == 200:
        data = response.json()
        print(f"\n响应体:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        checks = [
            ("object" in data and data["object"] == "list", "object='list'"),
            ("data" in data and isinstance(data["data"], list), "包含 data 数组"),
            ("model" in data, "包含 model"),
            ("usage" in data, "包含 usage"),
        ]

        for check, desc in checks:
            if check:
                print(f"✅ {desc}")
            else:
                print(f"❌ {desc}")

        return True
    else:
        print(f"\n❌ 请求失败: {response.text}")
        return False

def main():
    print("OpenAI 兼容 API 功能测试")
    print(f"Base URL: {BASE_URL}")
    print(f"API Token: {API_TOKEN[:20]}...")

    results = []

    try:
        results.append(("Models List", test_models_list()))
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        results.append(("Models List", False))

    try:
        results.append(("Model Get", test_model_get()))
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        results.append(("Model Get", False))

    try:
        results.append(("Chat Completions", test_chat_completions()))
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        results.append(("Chat Completions", False))

    try:
        results.append(("Chat Completions Stream", test_chat_completions_stream()))
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        results.append(("Chat Completions Stream", False))

    try:
        results.append(("Embeddings", test_embeddings()))
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        results.append(("Embeddings", False))

    # 总结
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{name}: {status}")

    passed = sum(1 for _, r in results if r)
    total = len(results)
    print(f"\n总计: {passed}/{total} 通过")

if __name__ == "__main__":
    main()
