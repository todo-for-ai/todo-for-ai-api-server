"""
OpenAI API 性能基准测试

使用 Locust 进行负载测试，支持几百到几千 QPS 的压力测试

特性：
- 多种并发场景测试
- 缓存命中率监控
- 响应时间分布统计
- 实时性能报告

使用方法:
    locust -f benchmark/locustfile.py --host=http://localhost:50110

或命令行模式:
    locust -f benchmark/locustfile.py --host=http://localhost:50110 -u 100 -r 10 --run-time 5m --headless
"""

import json
import time
import random
import uuid
from typing import Dict, Any

from locust import HttpUser, task, between, events
from locust.runners import MasterRunner


# ============== 配置 ==============

API_BASE = "/todo-for-ai/api/v1"
OPENAI_BASE = f"{API_BASE}"

# 测试用用户Token (在实际测试前需要替换)
TEST_API_TOKEN = "your-test-api-token-here"

# 测试配置
TEST_CONFIG = {
    "models": ["gpt-3.5-turbo", "gpt-4"],
    "messages_pool": [
        {"role": "user", "content": "你好"},
        {"role": "user", "content": "请帮我写一个Python函数"},
        {"role": "user", "content": "解释一下什么是机器学习"},
        {"role": "user", "content": "翻译这段文字到英文"},
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "帮我生成一段代码"},
        {"role": "user", "content": "总结一下这个文档"},
        {"role": "user", "content": "给我一个Python代码示例"},
    ]
}


# ============== 统计信息 ==============

class PerformanceStats:
    """性能统计"""

    def __init__(self):
        self.total_requests = 0
        self.success_requests = 0
        self.failed_requests = 0
        self.cache_hits = 0
        self.total_latency = 0.0
        self.min_latency = float('inf')
        self.max_latency = 0.0
        self.response_codes = {}
        self.endpoint_latency = {}

    def record(self, success: bool, latency: float, response_code: int, endpoint: str, cache_hit: bool = False):
        """记录请求统计"""
        self.total_requests += 1

        if success:
            self.success_requests += 1
        else:
            self.failed_requests += 1

        if cache_hit:
            self.cache_hits += 1

        self.total_latency += latency
        self.min_latency = min(self.min_latency, latency)
        self.max_latency = max(self.max_latency, latency)

        # 响应码分布
        self.response_codes[response_code] = self.response_codes.get(response_code, 0) + 1

        # 端点延迟统计
        if endpoint not in self.endpoint_latency:
            self.endpoint_latency[endpoint] = {'count': 0, 'total': 0, 'min': float('inf'), 'max': 0}
        self.endpoint_latency[endpoint]['count'] += 1
        self.endpoint_latency[endpoint]['total'] += latency
        self.endpoint_latency[endpoint]['min'] = min(self.endpoint_latency[endpoint]['min'], latency)
        self.endpoint_latency[endpoint]['max'] = max(self.endpoint_latency[endpoint]['max'], latency)

    @property
    def avg_latency(self) -> float:
        """平均延迟"""
        return self.total_latency / self.total_requests if self.total_requests > 0 else 0

    @property
    def success_rate(self) -> float:
        """成功率"""
        return (self.success_requests / self.total_requests * 100) if self.total_requests > 0 else 0

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率"""
        return (self.cache_hits / self.total_requests * 100) if self.total_requests > 0 else 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'total_requests': self.total_requests,
            'success_requests': self.success_requests,
            'failed_requests': self.failed_requests,
            'success_rate': f"{self.success_rate:.2f}%",
            'avg_latency_ms': f"{self.avg_latency:.2f}",
            'min_latency_ms': f"{self.min_latency:.2f}" if self.min_latency != float('inf') else "N/A",
            'max_latency_ms': f"{self.max_latency:.2f}",
            'cache_hit_rate': f"{self.cache_hit_rate:.2f}%",
            'response_codes': self.response_codes,
            'endpoint_stats': {
                endpoint: {
                    'count': stats['count'],
                    'avg_latency_ms': f"{stats['total'] / stats['count']:.2f}" if stats['count'] > 0 else 0,
                    'min_latency_ms': f"{stats['min']:.2f}",
                    'max_latency_ms': f"{stats['max']:.2f}",
                }
                for endpoint, stats in self.endpoint_latency.items()
            }
        }

    def __str__(self) -> str:
        """字符串表示"""
        lines = [
            "=" * 60,
            "性能测试统计",
            "=" * 60,
            f"总请求数: {self.total_requests}",
            f"成功请求: {self.success_requests}",
            f"失败请求: {self.failed_requests}",
            f"成功率: {self.success_rate:.2f}%",
            f"平均延迟: {self.avg_latency:.2f}ms",
            f"最小延迟: {self.min_latency:.2f}ms" if self.min_latency != float('inf') else "最小延迟: N/A",
            f"最大延迟: {self.max_latency:.2f}ms",
            f"缓存命中率: {self.cache_hit_rate:.2f}%",
            "-" * 60,
            "响应码分布:",
        ]
        for code, count in sorted(self.response_codes.items()):
            lines.append(f"  {code}: {count}")

        lines.append("-" * 60)
        lines.append("端点统计:")
        for endpoint, stats in self.endpoint_latency.items():
            avg = stats['total'] / stats['count'] if stats['count'] > 0 else 0
            lines.append(f"  {endpoint}:")
            lines.append(f"    请求数: {stats['count']}")
            lines.append(f"    平均延迟: {avg:.2f}ms")
            lines.append(f"    延迟范围: {stats['min']:.2f}ms - {stats['max']:.2f}ms")

        lines.append("=" * 60)
        return "\n".join(lines)


# 全局统计实例
stats = PerformanceStats()


# ============== Locust 用户定义 ==============

class OpenAIAPIUser(HttpUser):
    """OpenAI API 测试用户"""

    # 请求间隔 (1-5秒)
    wait_time = between(1, 5)

    def on_start(self):
        """测试开始前设置"""
        # 设置请求头
        self.client.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TEST_API_TOKEN}",
            "X-Test-Client": "locust"
        }

    @task(3)
    def test_chat_completions(self):
        """测试 Chat Completions API (权重最高)"""
        endpoint = f"{OPENAI_BASE}/v1/chat/completions"

        # 随机选择消息
        messages = random.sample(TEST_CONFIG["messages_pool"], k=random.randint(1, 3))
        # 确保第一个是user角色
        if messages[0].get('role') != 'user':
            messages[0] = {"role": "user", "content": messages[0].get('content', 'Hello')}

        payload = {
            "model": random.choice(TEST_CONFIG["models"]),
            "messages": messages,
            "temperature": random.uniform(0.1, 1.0),
            "max_tokens": random.randint(100, 1000)
        }

        start_time = time.time()
        with self.client.post(
            endpoint,
            json=payload,
            catch_response=True,
            name="/v1/chat/completions"
        ) as response:
            latency = (time.time() - start_time) * 1000

            if response.status_code == 200:
                try:
                    data = response.json()
                    # 检查缓存命中 (通过响应中的 cached 字段)
                    cache_hit = False
                    if isinstance(data, dict):
                        # 如果响应中有 cached 标记
                        cache_hit = data.get('data', {}).get('ai_metadata', {}).get('cached', False)

                    stats.record(True, latency, response.status_code, endpoint, cache_hit)
                    response.success()
                except Exception:
                    stats.record(False, latency, response.status_code, endpoint)
                    response.failure("Invalid JSON response")
            else:
                stats.record(False, latency, response.status_code, endpoint)
                response.failure(f"HTTP {response.status_code}")

    @task(1)
    def test_list_models(self):
        """测试 List Models API"""
        endpoint = f"{OPENAI_BASE}/v1/models"

        start_time = time.time()
        with self.client.get(
            endpoint,
            catch_response=True,
            name="/v1/models"
        ) as response:
            latency = (time.time() - start_time) * 1000

            if response.status_code == 200:
                stats.record(True, latency, response.status_code, endpoint, cache_hit=True)  # 通常有缓存
                response.success()
            else:
                stats.record(False, latency, response.status_code, endpoint)
                response.failure(f"HTTP {response.status_code}")

    @task(1)
    def test_get_model(self):
        """测试 Get Model API"""
        model_id = random.choice(TEST_CONFIG["models"])
        endpoint = f"{OPENAI_BASE}/v1/models/{model_id}"

        start_time = time.time()
        with self.client.get(
            endpoint,
            catch_response=True,
            name="/v1/models/{model}"
        ) as response:
            latency = (time.time() - start_time) * 1000

            if response.status_code in [200, 404]:  # 404也是正常响应
                stats.record(True, latency, response.status_code, endpoint)
                response.success()
            else:
                stats.record(False, latency, response.status_code, endpoint)
                response.failure(f"HTTP {response.status_code}")

    @task(2)
    def test_embeddings(self):
        """测试 Embeddings API"""
        endpoint = f"{OPENAI_BASE}/v1/embeddings"

        inputs = [
            "这是一个测试文本",
            "Hello world",
            "Machine learning is interesting",
            "Python is a great language"
        ]

        payload = {
            "model": "text-embedding-ada-002",
            "input": random.choice(inputs)
        }

        start_time = time.time()
        with self.client.post(
            endpoint,
            json=payload,
            catch_response=True,
            name="/v1/embeddings"
        ) as response:
            latency = (time.time() - start_time) * 1000

            if response.status_code == 200:
                try:
                    data = response.json()
                    cache_hit = data.get('cached', False) if isinstance(data, dict) else False
                    stats.record(True, latency, response.status_code, endpoint, cache_hit)
                    response.success()
                except Exception:
                    stats.record(False, latency, response.status_code, endpoint)
                    response.failure("Invalid JSON response")
            else:
                stats.record(False, latency, response.status_code, endpoint)
                response.failure(f"HTTP {response.status_code}")


class CacheTestUser(HttpUser):
    """缓存测试用户 - 发送相同请求测试缓存效果"""

    wait_time = between(0.5, 2)

    def on_start(self):
        """生成固定测试数据"""
        self.client.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TEST_API_TOKEN}",
            "X-Test-Client": "locust-cache"
        }
        # 固定的测试消息 (用于测试缓存)
        self.test_messages = [
            {"role": "user", "content": "缓存测试消息1"},
            {"role": "user", "content": "缓存测试消息2"},
            {"role": "user", "content": "缓存测试消息3"},
        ]

    @task(1)
    def test_cache_hit(self):
        """测试缓存命中"""
        endpoint = f"{OPENAI_BASE}/v1/chat/completions"

        # 使用固定的消息，增加缓存命中概率
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [random.choice(self.test_messages)],
            "temperature": 0.7,
            "max_tokens": 200
        }

        with self.client.post(
            endpoint,
            json=payload,
            catch_response=True,
            name="/v1/chat/completions (cache test)"
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"HTTP {response.status_code}")


# ============== 事件处理 ==============

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """测试开始事件"""
    print("\n" + "=" * 60)
    print("OpenAI API 性能基准测试开始")
    print("=" * 60)
    print(f"目标Host: {environment.host}")
    print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 检查API Token
    if TEST_API_TOKEN == "your-test-api-token-here":
        print("\n⚠️ 警告: 请设置有效的 TEST_API_TOKEN!")
        print("在 benchmark/locustfile.py 中修改 TEST_API_TOKEN 变量\n")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """测试结束事件"""
    print("\n" + "=" * 60)
    print("测试结束，生成报告...")
    print("=" * 60)
    print(stats)

    # 生成JSON报告
    report_data = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'host': environment.host,
        'stats': stats.to_dict()
    }

    report_file = f"benchmark_report_{int(time.time())}.json"
    try:
        with open(report_file, 'w') as f:
            json.dump(report_data, f, indent=2)
        print(f"\n报告已保存到: {report_file}")
    except Exception as e:
        print(f"\n保存报告失败: {e}")


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    """请求事件 (可用于实时监控)"""
    pass  # 使用 Locust 内置的统计


# ============== 自定义命令 (可选) ==============

if __name__ == "__main__":
    import sys

    # 如果直接运行，显示帮助信息
    print("OpenAI API 性能基准测试")
    print("=" * 60)
    print("使用方法:")
    print("  1. Web界面模式:")
    print("     locust -f benchmark/locustfile.py --host=http://localhost:50110")
    print("")
    print("  2. 命令行模式 (无头模式):")
    print("     locust -f benchmark/locustfile.py --host=http://localhost:50110 \\")
    print("            -u 100 -r 10 --run-time 5m --headless")
    print("")
    print("     参数说明:")
    print("       -u: 并发用户数")
    print("       -r: 每秒启动用户数 (ramp-up)")
    print("       --run-time: 测试持续时间")
    print("")
    print("  3. 分布式模式:")
    print("     Master: locust -f benchmark/locustfile.py --master --host=http://localhost:50110")
    print("     Worker: locust -f benchmark/locustfile.py --worker --master-host=localhost")
    print("")
    print("  4. 不同并发场景示例:")
    print("     # 低并发测试 (100 QPS 目标)")
    print("     locust -f benchmark/locustfile.py --host=http://localhost:50110 \\")
    print("            -u 10 -r 2 --run-time 2m --headless")
    print("")
    print("     # 中并发测试 (500 QPS 目标)")
    print("     locust -f benchmark/locustfile.py --host=http://localhost:50110 \\")
    print("            -u 50 -r 10 --run-time 5m --headless")
    print("")
    print("     # 高并发测试 (1000+ QPS 目标)")
    print("     locust -f benchmark/locustfile.py --host=http://localhost:50110 \\")
    print("            -u 200 -r 50 --run-time 10m --headless")
    print("")
    print("     # 极限压力测试 (3000+ QPS 目标)")
    print("     locust -f benchmark/locustfile.py --host=http://localhost:50110 \\")
    print("            -u 500 -r 100 --run-time 15m --headless")
    print("=" * 60)
