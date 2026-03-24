"""
简单的基准测试脚本 (无需 Locust)

使用 Python requests 和 threading 进行基本性能测试

使用方法:
    python benchmark/simple_benchmark.py --host http://localhost:50110 --token your-token
"""

import argparse
import json
import time
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests
import sys


class SimpleBenchmark:
    """简单基准测试器"""

    def __init__(self, host: str, token: str, timeout: int = 30):
        self.host = host.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.results = []
        self.lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        })

    def make_request(self, endpoint: str, method: str = 'GET', payload: dict = None) -> dict:
        """发送单个请求"""
        url = f"{self.host}{endpoint}"
        start_time = time.time()

        try:
            if method == 'GET':
                response = self.session.get(url, timeout=self.timeout)
            else:
                response = self.session.post(url, json=payload, timeout=self.timeout)

            elapsed = (time.time() - start_time) * 1000  # ms

            return {
                'success': response.status_code == 200,
                'status_code': response.status_code,
                'latency_ms': elapsed,
                'endpoint': endpoint,
                'error': None
            }
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            return {
                'success': False,
                'status_code': 0,
                'latency_ms': elapsed,
                'endpoint': endpoint,
                'error': str(e)
            }

    def test_endpoint(self, endpoint: str, method: str = 'GET', payload: dict = None, iterations: int = 100):
        """测试单个端点"""
        results = []

        for _ in range(iterations):
            result = self.make_request(endpoint, method, payload)
            results.append(result)

        return results

    def test_endpoint_concurrent(self, endpoint: str, method: str = 'GET', payload: dict = None,
                                  total_requests: int = 1000, concurrency: int = 50):
        """并发测试端点"""
        results = []
        completed = 0
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(self.make_request, endpoint, method, payload)
                for _ in range(total_requests)
            ]

            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                completed += 1

                # 实时进度
                if completed % 100 == 0:
                    elapsed = time.time() - start_time
                    qps = completed / elapsed if elapsed > 0 else 0
                    print(f"  进度: {completed}/{total_requests} ({completed/total_requests*100:.1f}%) - QPS: {qps:.1f}")

        return results

    def run_chat_completion_test(self, total_requests: int = 1000, concurrency: int = 50):
        """运行 Chat Completion 测试"""
        print(f"\n{'='*60}")
        print("Chat Completion API 测试")
        print(f"{'='*60}")
        print(f"总请求数: {total_requests}")
        print(f"并发数: {concurrency}")

        endpoint = "/todo-for-ai/api/v1/v1/chat/completions"
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Hello, how are you?"}],
            "temperature": 0.7,
            "max_tokens": 200
        }

        start_time = time.time()
        results = self.test_endpoint_concurrent(
            endpoint, 'POST', payload, total_requests, concurrency
        )
        total_time = time.time() - start_time

        return self._analyze_results(results, total_time)

    def run_models_test(self, total_requests: int = 1000, concurrency: int = 50):
        """运行 Models 测试"""
        print(f"\n{'='*60}")
        print("Models API 测试")
        print(f"{'='*60}")
        print(f"总请求数: {total_requests}")
        print(f"并发数: {concurrency}")

        endpoint = "/todo-for-ai/api/v1/v1/models"

        start_time = time.time()
        results = self.test_endpoint_concurrent(
            endpoint, 'GET', None, total_requests, concurrency
        )
        total_time = time.time() - start_time

        return self._analyze_results(results, total_time)

    def _analyze_results(self, results: list, total_time: float) -> dict:
        """分析测试结果"""
        if not results:
            return {}

        latencies = [r['latency_ms'] for r in results]
        success_count = sum(1 for r in results if r['success'])
        error_count = len(results) - success_count

        # 统计
        analysis = {
            'total_requests': len(results),
            'success_count': success_count,
            'error_count': error_count,
            'success_rate': success_count / len(results) * 100,
            'total_time_seconds': total_time,
            'qps': len(results) / total_time if total_time > 0 else 0,
            'latency_ms': {
                'min': min(latencies),
                'max': max(latencies),
                'avg': statistics.mean(latencies),
                'median': statistics.median(latencies),
                'p95': sorted(latencies)[int(len(latencies) * 0.95)],
                'p99': sorted(latencies)[int(len(latencies) * 0.99)],
            }
        }

        return analysis

    def print_report(self, name: str, results: dict):
        """打印测试报告"""
        print(f"\n{name} 测试结果:")
        print(f"{'-'*60}")
        print(f"总请求数: {results['total_requests']}")
        print(f"成功数: {results['success_count']}")
        print(f"失败数: {results['error_count']}")
        print(f"成功率: {results['success_rate']:.2f}%")
        print(f"总耗时: {results['total_time_seconds']:.2f}s")
        print(f"QPS: {results['qps']:.2f}")
        print(f"延迟统计 (ms):")
        lat = results['latency_ms']
        print(f"  最小: {lat['min']:.2f}")
        print(f"  最大: {lat['max']:.2f}")
        print(f"  平均: {lat['avg']:.2f}")
        print(f"  中位数: {lat['median']:.2f}")
        print(f"  P95: {lat['p95']:.2f}")
        print(f"  P99: {lat['p99']:.2f}")
        print(f"{'-'*60}")


    def run_all_tests(self, config: dict):
        """运行所有测试"""
        print("\n" + "="*60)
        print("OpenAI API 性能基准测试")
        print("="*60)
        print(f"目标Host: {self.host}")
        print(f"测试时间: {datetime.now().isoformat()}")
        print("="*60)

        all_results = {}

        # 1. 低并发测试
        if 'low' in config:
            print("\n" + "="*60)
            print("场景 1: 低并发测试 (目标 100 QPS)")
            print("="*60)
            low_config = config['low']
            all_results['chat_low'] = self.run_chat_completion_test(
                low_config['requests'], low_config['concurrency']
            )
            self.print_report("Chat Completion", all_results['chat_low'])

            all_results['models_low'] = self.run_models_test(
                low_config['requests'], low_config['concurrency']
            )
            self.print_report("Models", all_results['models_low'])

        # 2. 中并发测试
        if 'medium' in config:
            print("\n" + "="*60)
            print("场景 2: 中并发测试 (目标 500 QPS)")
            print("="*60)
            medium_config = config['medium']
            all_results['chat_medium'] = self.run_chat_completion_test(
                medium_config['requests'], medium_config['concurrency']
            )
            self.print_report("Chat Completion", all_results['chat_medium'])

            all_results['models_medium'] = self.run_models_test(
                medium_config['requests'], medium_config['concurrency']
            )
            self.print_report("Models", all_results['models_medium'])

        # 3. 高并发测试
        if 'high' in config:
            print("\n" + "="*60)
            print("场景 3: 高并发测试 (目标 1000+ QPS)")
            print("="*60)
            high_config = config['high']
            all_results['chat_high'] = self.run_chat_completion_test(
                high_config['requests'], high_config['concurrency']
            )
            self.print_report("Chat Completion", all_results['chat_high'])

            all_results['models_high'] = self.run_models_test(
                high_config['requests'], high_config['concurrency']
            )
            self.print_report("Models", all_results['models_high'])

        # 4. 极限压力测试
        if 'extreme' in config:
            print("\n" + "="*60)
            print("场景 4: 极限压力测试 (目标 3000+ QPS)")
            print("="*60)
            extreme_config = config['extreme']
            all_results['chat_extreme'] = self.run_chat_completion_test(
                extreme_config['requests'], extreme_config['concurrency']
            )
            self.print_report("Chat Completion", all_results['chat_extreme'])

            all_results['models_extreme'] = self.run_models_test(
                extreme_config['requests'], extreme_config['concurrency']
            )
            self.print_report("Models", all_results['models_extreme'])

        # 生成最终报告
        self._generate_final_report(all_results)

        return all_results

    def _generate_final_report(self, all_results: dict):
        """生成最终报告"""
        print("\n" + "="*60)
        print("最终测试报告")
        print("="*60)

        # 汇总所有结果
        total_requests = sum(r['total_requests'] for r in all_results.values())
        total_success = sum(r['success_count'] for r in all_results.values())
        avg_qps = statistics.mean([r['qps'] for r in all_results.values()])

        print(f"总请求数: {total_requests}")
        print(f"总成功数: {total_success}")
        print(f"整体成功率: {total_success/total_requests*100:.2f}%")
        print(f"平均 QPS: {avg_qps:.2f}")

        # 按场景分析
        print("\n按场景分析:")
        for scenario in ['low', 'medium', 'high', 'extreme']:
            chat_key = f'chat_{scenario}'
            models_key = f'models_{scenario}'

            if chat_key in all_results:
                chat_r = all_results[chat_key]
                models_r = all_results[models_key]
                avg_qps_scene = (chat_r['qps'] + models_r['qps']) / 2

                print(f"\n  {scenario.upper()} 场景:")
                print(f"    Chat QPS: {chat_r['qps']:.2f}")
                print(f"    Models QPS: {models_r['qps']:.2f}")
                print(f"    平均 QPS: {avg_qps_scene:.2f}")
                print(f"    Chat P95延迟: {chat_r['latency_ms']['p95']:.2f}ms")
                print(f"    Models P95延迟: {models_r['latency_ms']['p95']:.2f}ms")

        print("="*60)

        # 保存JSON报告
        report_file = f"benchmark_report_{int(time.time())}.json"
        try:
            with open(report_file, 'w') as f:
                json.dump({
                    'timestamp': datetime.now().isoformat(),
                    'host': self.host,
                    'results': all_results
                }, f, indent=2)
            print(f"\n详细报告已保存到: {report_file}")
        except Exception as e:
            print(f"\n保存报告失败: {e}")


def main():
    parser = argparse.ArgumentParser(description='OpenAI API 性能基准测试')
    parser.add_argument('--host', default='http://localhost:50110', help='API host')
    parser.add_argument('--token', required=True, help='API token')
    parser.add_argument('--scenario', choices=['low', 'medium', 'high', 'extreme', 'all'],
                       default='all', help='测试场景')
    parser.add_argument('--requests', type=int, default=1000, help='每个场景的请求数')
    parser.add_argument('--concurrency', type=int, default=50, help='并发数')

    args = parser.parse_args()

    if not args.token:
        print("错误: 必须提供 --token 参数")
        sys.exit(1)

    # 构建测试配置
    if args.scenario == 'all':
        config = {
            'low': {'requests': 500, 'concurrency': 10},
            'medium': {'requests': 2000, 'concurrency': 50},
            'high': {'requests': 5000, 'concurrency': 100},
            # 'extreme': {'requests': 10000, 'concurrency': 300},
        }
    else:
        scenario_config = {
            'low': {'requests': 500, 'concurrency': 10},
            'medium': {'requests': 2000, 'concurrency': 50},
            'high': {'requests': 5000, 'concurrency': 100},
            'extreme': {'requests': 10000, 'concurrency': 300},
        }
        config = {args.scenario: scenario_config[args.scenario]}

    # 运行测试
    benchmark = SimpleBenchmark(args.host, args.token)
    benchmark.run_all_tests(config)


if __name__ == '__main__':
    main()
