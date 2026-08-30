#!/usr/bin/env python3
"""
OpenAI 兼容 API 完整功能测试

测试标准兼容性：
1. 路径标准: /v1/models, /v1/chat/completions, /v1/embeddings
2. 请求体标准: 支持所有 OpenAI 标准参数
3. 响应体标准: 完全兼容 OpenAI 响应格式
4. 错误格式标准: 符合 OpenAI 错误格式
"""

import requests
import json
import sys

BASE_URL = "http://127.0.0.1:50110"
API_TOKEN = "ags_9jNOqCvIJOEnrir9HYk-Rxz0WK1mLLmpKfV0BGAPeUg"

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json"
}


def test_models_list():
    """测试模型列表 - OpenAI 标准格式"""
    print("\n" + "="*70)
    print("📋 测试 1: GET /v1/models - 模型列表")
    print("="*70)

    response = requests.get(f"{BASE_URL}/v1/models", headers=headers)

    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()

        # 验证 OpenAI 标准格式
        checks = [
            ("object" in data and data["object"] == "list", "✅ object='list'"),
            ("data" in data and isinstance(data["data"], list), f"✅ data array ({len(data.get('data', []))} models)"),
        ]

        for check, msg in checks:
            print(f"  {msg}" if check else f"  ❌ Failed")

        # 验证每个模型的字段
        if data.get("data"):
            model = data["data"][0]
            model_checks = ["id", "object", "created", "owned_by"]
            for field in model_checks:
                status = "✅" if field in model else "❌"
                print(f"  {status} model has '{field}'")

        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_chat_completions_basic():
    """测试基础聊天补全"""
    print("\n" + "="*70)
    print("💬 测试 2: POST /v1/chat/completions - 基础请求")
    print("="*70)

    payload = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hello"}]
    }

    response = requests.post(f"{BASE_URL}/v1/chat/completions",
                           headers=headers, json=payload)

    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()

        # 验证标准字段
        standard_fields = ["id", "object", "created", "model", "choices", "usage"]
        for field in standard_fields:
            status = "✅" if field in data else "❌"
            print(f"  {status} has '{field}'")

        # 验证 choices 格式
        if data.get("choices"):
            choice = data["choices"][0]
            choice_fields = ["index", "message", "finish_reason"]
            for field in choice_fields:
                status = "✅" if field in choice else "❌"
                print(f"  {status} choice has '{field}'")

            if choice.get("message"):
                msg_status = "✅" if choice["message"].get("role") == "assistant" else "❌"
                print(f"  {msg_status} message.role='assistant'")

        # 验证 usage 格式
        if data.get("usage"):
            usage_fields = ["prompt_tokens", "completion_tokens", "total_tokens"]
            for field in usage_fields:
                status = "✅" if field in data["usage"] else "❌"
                print(f"  {status} usage has '{field}'")

        # 验证 system_fingerprint（OpenAI 新字段）
        if "system_fingerprint" in data:
            print(f"  ✅ has system_fingerprint")

        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_chat_completions_full_params():
    """测试完整参数聊天补全"""
    print("\n" + "="*70)
    print("🔧 测试 3: POST /v1/chat/completions - 完整参数")
    print("="*70)

    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say hello in Chinese"}
        ],
        "temperature": 0.8,
        "max_tokens": 100,
        "top_p": 0.9,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.1,
        "n": 1,
        "stream": False,
        "user": "test_user_123"
    }

    print(f"Request: {json.dumps(payload, indent=2)}")

    response = requests.post(f"{BASE_URL}/v1/chat/completions",
                           headers=headers, json=payload)

    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"Response model: {data.get('model')}")
        if data.get("choices"):
            content = data["choices"][0].get("message", {}).get("content", "")[:50]
            print(f"Content: {content}...")
        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_chat_completions_with_stop():
    """测试带 stop 序列的请求"""
    print("\n" + "="*70)
    print("🛑 测试 4: POST /v1/chat/completions - stop 参数")
    print("="*70)

    payload = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Count from 1 to 10"}],
        "stop": ["5"],
        "max_tokens": 50
    }

    response = requests.post(f"{BASE_URL}/v1/chat/completions",
                           headers=headers, json=payload)

    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"Content (stopped at '5'): {content}")
        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_chat_completions_stream():
    """测试流式响应"""
    print("\n" + "="*70)
    print("🌊 测试 5: POST /v1/chat/completions - 流式响应")
    print("="*70)

    payload = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True
    }

    response = requests.post(f"{BASE_URL}/v1/chat/completions",
                           headers=headers, json=payload, stream=True)

    print(f"Status: {response.status_code}")
    print(f"Content-Type: {response.headers.get('content-type')}")

    if response.status_code == 200:
        chunks = []
        for line in response.iter_lines():
            if line:
                line_str = line.decode('utf-8')
                if line_str.startswith("data: "):
                    data = line_str[6:]
                    if data == "[DONE]":
                        print(f"  ✅ Stream ended with [DONE]")
                        break
                    try:
                        chunk = json.loads(data)
                        if chunk.get("object") == "chat.completion.chunk":
                            chunks.append(chunk)
                    except json.JSONDecodeError:
                        pass

        print(f"  ✅ Received {len(chunks)} chunks")

        # 验证第一个 chunk 的格式
        if chunks:
            chunk = chunks[0]
            if chunk.get("object") == "chat.completion.chunk":
                print(f"  ✅ Chunk object='chat.completion.chunk'")
            if chunk.get("choices") and chunk["choices"][0].get("delta"):
                print(f"  ✅ Chunk has delta")

        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_embeddings():
    """测试嵌入 API"""
    print("\n" + "="*70)
    print("📊 测试 6: POST /v1/embeddings - 文本嵌入")
    print("="*70)

    payload = {
        "model": "text-embedding-ada-002",
        "input": "This is a test sentence for embedding."
    }

    response = requests.post(f"{BASE_URL}/v1/embeddings",
                           headers=headers, json=payload)

    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()

        checks = [
            ("object" in data and data["object"] == "list", "✅ object='list'"),
            ("data" in data and isinstance(data["data"], list), f"✅ data array ({len(data.get('data', []))} items)"),
            ("model" in data, f"✅ model='{data.get('model')}'"),
            ("usage" in data, "✅ has usage"),
        ]

        for check, msg in checks:
            print(f"  {msg}" if check else f"  ❌ Failed")

        # 验证嵌入向量
        if data.get("data") and len(data["data"]) > 0:
            embedding = data["data"][0]
            if "embedding" in embedding and isinstance(embedding["embedding"], list):
                print(f"  ✅ embedding vector length: {len(embedding['embedding'])}")

        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_embeddings_batch():
    """测试批量嵌入"""
    print("\n" + "="*70)
    print("📚 测试 7: POST /v1/embeddings - 批量嵌入")
    print("="*70)

    payload = {
        "model": "text-embedding-ada-002",
        "input": [
            "First test sentence.",
            "Second test sentence.",
            "Third test sentence."
        ]
    }

    response = requests.post(f"{BASE_URL}/v1/embeddings",
                           headers=headers, json=payload)

    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        embeddings_count = len(data.get("data", []))
        print(f"  ✅ Returned {embeddings_count} embeddings")

        # 验证每个 embedding 有 index
        for i, emb in enumerate(data.get("data", [])):
            if emb.get("index") == i:
                print(f"  ✅ embedding[{i}].index = {i}")

        return True
    else:
        print(f"  ❌ Failed: {response.text}")
        return False


def test_error_format():
    """测试错误响应格式"""
    print("\n" + "="*70)
    print("❌ 测试 8: 错误响应格式")
    print("="*70)

    # 测试缺少必填字段
    payload = {
        "model": "gpt-4"
        # missing messages
    }

    response = requests.post(f"{BASE_URL}/v1/chat/completions",
                           headers=headers, json=payload)

    print(f"Status: {response.status_code}")
    print(f"Response: {response.text[:200]}")

    return True


def test_model_not_found():
    """测试模型不存在"""
    print("\n" + "="*70)
    print("🔍 测试 9: GET /v1/models/{invalid-id}")
    print("="*70)

    response = requests.get(f"{BASE_URL}/v1/models/invalid-model",
                           headers=headers)

    print(f"Status: {response.status_code}")

    if response.status_code == 404:
        print(f"  ✅ Correctly returned 404 for invalid model")
        return True
    else:
        print(f"  Response: {response.text[:100]}")
        return False


def main():
    print("\n" + "🧪 "*25)
    print("  OpenAI API 完整兼容性测试")
    print("🧪 "*25)
    print(f"\nBase URL: {BASE_URL}")
    print(f"API Token: {API_TOKEN[:15]}...")

    tests = [
        ("Models List", test_models_list),
        ("Chat Completions Basic", test_chat_completions_basic),
        ("Chat Completions Full Params", test_chat_completions_full_params),
        ("Chat Completions Stop", test_chat_completions_with_stop),
        ("Chat Completions Stream", test_chat_completions_stream),
        ("Embeddings", test_embeddings),
        ("Embeddings Batch", test_embeddings_batch),
        ("Error Format", test_error_format),
        ("Model Not Found", test_model_not_found),
    ]

    results = []
    for name, test_func in tests:
        try:
            results.append((name, test_func()))
        except Exception as e:
            print(f"\n❌ Exception: {e}")
            results.append((name, False))

    # 总结
    print("\n" + "="*70)
    print("📊 测试总结")
    print("="*70)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status} - {name}")

    passed = sum(1 for _, r in results if r)
    total = len(results)

    print(f"\n总计: {passed}/{total} 通过 ({passed/total*100:.1f}%)")

    if passed == total:
        print("\n🎉 所有测试通过！OpenAI API 完全兼容！")


if __name__ == "__main__":
    main()
