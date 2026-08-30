# OpenAI API 兼容层性能优化文档

## 1. 概述

本实现提供了一个与 OpenAI API 兼容的高性能后端，支持几百到几千 QPS 的高并发场景。

## 2. 主要特性

### 2.1 OpenAI API 兼容路由

支持以下端点:
- `/v1/chat/completions` - Chat Completions API
- `/v1/models` - List/Get Models API
- `/v1/embeddings` - Embeddings API
- `/v1/usage` - 使用统计 (扩展)
- `/v1/cache/invalidate` - 缓存管理 (扩展)

### 2.2 Redis 缓存层

**双级缓存策略:**
- **L1 (本地缓存)**: 进程内内存缓存，响应时间微秒级
- **L2 (Redis缓存)**: 分布式缓存，支持多实例共享

**缓存一致性保障:**
- 分布式锁防止缓存击穿
- 缓存失效广播机制
- 原子性锁操作 (Lua脚本)
- 自动锁续期

### 2.3 性能优化

**高并发优化:**
- 连接池复用
- 异步日志记录
- 批量操作支持
- 缓存预热
- 自动降级

## 3. 使用示例

### 3.1 使用缓存装饰器

```python
from services.redis_cache_service import cached

@cached(namespace='openai', ttl=300)
def get_model_info(model_id):
    # 数据库查询或其他耗时操作
    return Model.query.get(model_id)

# 手动使缓存失效
get_model_info.cache_delete('gpt-4')

# 使所有模型缓存失效
get_model_info.cache_invalidate()
```

### 3.2 批量操作

```python
from services.redis_cache_service import get_cache

cache = get_cache()

# 批量获取
results = cache.mget(
    namespace='embeddings',
    identifiers=['text1', 'text2', 'text3'],
    fallback=lambda missing: {m: compute_embedding(m) for m in missing}
)

# 批量设置
data_map = {'key1': value1, 'key2': value2}
cache.mset('namespace', data_map, ttl=300)
```

### 3.3 分布式锁

```python
from services.redis_cache_service import DistributedLock, get_cache

redis_client = get_cache()._get_redis()
lock = DistributedLock(redis_client, 'resource_key', timeout=30)

with lock:
    # 临界区代码
    process_exclusive_resource()
```

## 4. 缓存一致性策略

### 4.1 写策略

- **Write-Through**: 同时写入缓存和数据库
- **Write-Behind**: 异步写入数据库 (高写入场景)

### 4.2 读策略

- **Cache-Aside**: 应用负责缓存管理
- **Read-Through**: 缓存自动加载

### 4.3 失效策略

- **主动失效**: 数据更新时主动清除缓存
- **被动失效**: TTL过期
- **广播失效**: Redis Pub/Sub通知所有节点

## 5. 数据库索引

运行索引优化脚本:

```bash
python migrations/add_openai_api_indexes.py
```

主要索引:
- `idx_ai_logs_user_time` - 用户请求日志时间范围查询
- `idx_ai_logs_feature_time` - 功能请求日志时间范围查询
- `idx_ai_logs_cache_hit` - 缓存命中率统计
- `idx_api_tokens_token_hash` - Token快速验证
- `idx_api_tokens_expires` - 过期Token清理

## 6. 基准测试

### 6.1 使用 Locust (推荐)

安装依赖:
```bash
pip install locust
```

运行测试:
```bash
# Web界面模式
locust -f benchmark/locustfile.py --host=http://localhost:50110

# 命令行模式
locust -f benchmark/locustfile.py --host=http://localhost:50110 \
    -u 100 -r 10 --run-time 5m --headless
```

### 6.2 使用简单脚本

```bash
python benchmark/simple_benchmark.py \
    --host http://localhost:50110 \
    --token your-api-token \
    --scenario all
```

### 6.3 测试场景

| 场景 | 并发数 | 目标QPS | 运行时间 |
|------|--------|---------|----------|
| Low | 10 | 100 | 2分钟 |
| Medium | 50 | 500 | 5分钟 |
| High | 100 | 1000 | 10分钟 |
| Extreme | 300 | 3000 | 15分钟 |

## 7. 性能调优建议

### 7.1 Redis 配置

```conf
# redis.conf
maxmemory 2gb
maxmemory-policy allkeys-lru
tcp-keepalive 60
timeout 300
```

### 7.2 应用配置

```python
# config.py
SQLALCHEMY_ENGINE_OPTIONS = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_timeout': 30,
    'pool_size': 20,
    'max_overflow': 30,
}
```

### 7.3 Gunicorn 配置

```bash
gunicorn -w 8 -k gevent --worker-connections 1000 \
    --max-requests 1000 --max-requests-jitter 50 \
    --timeout 60 --keep-alive 2 \
    --bind 0.0.0.0:50110 app:app
```

## 8. 监控指标

### 8.1 缓存指标

- 缓存命中率
- 平均延迟
- Redis连接数
- 内存使用量

### 8.2 API指标

- QPS (每秒请求数)
- 响应时间分布 (P50, P95, P99)
- 错误率
- 并发连接数

### 8.3 数据库指标

- 查询响应时间
- 连接池使用率
- 慢查询数量

## 9. 故障排除

### 9.1 缓存击穿

现象: 缓存未命中导致数据库压力突增

解决方案:
- 启用分布式锁
- 热点数据预加载
- 设置热点key永不过期

### 9.2 缓存穿透

现象: 查询不存在的数据

解决方案:
- 空值缓存
- 布隆过滤器
- 参数校验

### 9.3 缓存雪崩

现象: 大量key同时过期

解决方案:
- 随机TTL
- 多级缓存
- 熔断降级

## 10. API 文档

### 10.1 Chat Completions

```http
POST /todo-for-ai/api/v1/v1/chat/completions
Authorization: Bearer {token}
Content-Type: application/json

{
    "model": "gpt-3.5-turbo",
    "messages": [
        {"role": "user", "content": "Hello!"}
    ],
    "temperature": 0.7,
    "max_tokens": 200,
    "stream": false
}
```

响应:
```json
{
    "id": "chatcmpl-...",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "gpt-3.5-turbo",
    "choices": [...],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30
    }
}
```

### 10.2 Cache Management

```http
POST /todo-for-ai/api/v1/v1/cache/invalidate
Authorization: Bearer {token}
Content-Type: application/json

{
    "feature": "chat"  // 可选: chat, embedding, models, all
}
```
