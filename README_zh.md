# Todo for AI API 服务器

**中文版本** | [English](README.md)

Todo for AI 任务管理系统的 RESTful API 服务器，基于 Flask 构建，专为 AI 助手集成而设计。

> 🚀 **立即体验**: 访问 [https://todo4ai.org/](https://todo4ai.org/) 体验我们的产品！

## ✨ 功能特性

- 🔐 **多重认证**: 支持 JWT 令牌和 API 令牌
- 🌐 **OAuth 集成**: Google 和 GitHub OAuth 登录
- 📊 **项目管理**: 完整的项目生命周期管理
- ✅ **任务管理**: 高级任务跟踪，支持状态、优先级和分配
- 🤖 **AI 集成**: 支持 MCP (模型上下文协议) 的 AI 助手
- 📝 **上下文规则**: 自定义提示和上下文管理
- 🔒 **安全性**: 令牌加密、CORS 保护和安全会话管理
- 📈 **仪表板**: 实时统计和分析
- 🔍 **搜索和过滤**: 高级过滤和排序功能
- 📱 **RESTful API**: 清晰、文档完善的 REST 端点

## 🚀 快速开始

### 前置要求

- Python 3.8+
- MySQL 5.7+ 或 8.0+
- pip 或 conda

### 安装

1. **克隆仓库**
```bash
git clone https://github.com/todo-for-ai/todo-for-ai-api-server.git
cd todo-for-ai-api-server
```

2. **创建虚拟环境**
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

3. **安装依赖**
```bash
pip install -r requirements.txt
```

4. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件配置您的设置
```

5. **初始化数据库**
```bash
python migrations/create_database.py
```

6. **运行服务器**
```bash
python app.py
```

API 服务器将在 `http://localhost:50110` 上运行

## 🔧 配置

### 环境变量

创建 `.env` 文件并配置以下变量：

```bash
# 数据库配置
DATABASE_URL=mysql+pymysql://username:password@localhost:3306/todo_for_ai

# Flask 配置
SECRET_KEY=your-secret-key-here
JWT_SECRET_KEY=your-jwt-secret-key-here
FLASK_ENV=development

# OAuth 配置
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GITHUB_CLIENT_ID=your-github-client-id
GITHUB_CLIENT_SECRET=your-github-client-secret

# 服务器配置
HOST=127.0.0.1
PORT=50110

# CORS 配置
CORS_ORIGINS=http://localhost:5173,http://localhost:50111

# 日志配置
LOG_LEVEL=INFO
LOG_FILE=app.log
```

### OAuth 设置

#### Google OAuth
1. 访问 [Google Cloud Console](https://console.cloud.google.com/)
2. 创建新项目或选择现有项目
3. 启用 Google+ API
4. 创建 OAuth 2.0 凭据
5. 添加授权重定向 URI: `http://localhost:50110/todo-for-ai/api/v1/auth/google/callback`

#### GitHub OAuth
1. 访问 [GitHub Developer Settings](https://github.com/settings/developers)
2. 创建新的 OAuth 应用
3. 设置授权回调 URL: `http://localhost:50110/todo-for-ai/api/v1/auth/github/callback`

## 📚 API 文档

### 基础 URL
```
http://localhost:50110/todo-for-ai/api/v1
```

### 认证

API 支持两种认证方式：

1. **JWT 令牌** (用于 Web 应用)
2. **API 令牌** (用于 AI 助手和集成)

#### 使用 JWT 令牌
```bash
# 登录获取 JWT 令牌
curl -X POST http://localhost:50110/todo-for-ai/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "password"}'

# 在请求中使用令牌
curl -H "Authorization: Bearer <jwt_token>" \
  http://localhost:50110/todo-for-ai/api/v1/projects
```

#### 使用 API 令牌
```bash
# 创建 API 令牌
curl -X POST http://localhost:50110/todo-for-ai/api/v1/api-tokens \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "My API Token", "expires_in_days": 30}'

# 在请求中使用 API 令牌
curl -H "Authorization: Bearer <api_token>" \
  http://localhost:50110/todo-for-ai/api/v1/projects
```

### 核心端点

#### 健康检查
```bash
GET /health
GET /todo-for-ai/api/v1/health
```

#### 认证
```bash
POST /todo-for-ai/api/v1/auth/login
POST /todo-for-ai/api/v1/auth/logout
GET  /todo-for-ai/api/v1/auth/google
GET  /todo-for-ai/api/v1/auth/github
```

#### 项目
```bash
GET    /todo-for-ai/api/v1/projects          # 列出项目
POST   /todo-for-ai/api/v1/projects          # 创建项目
GET    /todo-for-ai/api/v1/projects/{id}     # 获取项目
PUT    /todo-for-ai/api/v1/projects/{id}     # 更新项目
DELETE /todo-for-ai/api/v1/projects/{id}     # 删除项目
```

#### 任务
```bash
GET    /todo-for-ai/api/v1/tasks             # 列出任务
POST   /todo-for-ai/api/v1/tasks             # 创建任务
GET    /todo-for-ai/api/v1/tasks/{id}        # 获取任务
PUT    /todo-for-ai/api/v1/tasks/{id}        # 更新任务
DELETE /todo-for-ai/api/v1/tasks/{id}        # 删除任务
POST   /todo-for-ai/api/v1/tasks/{id}/feedback # 提交反馈
```

#### MCP 集成
```bash
GET    /todo-for-ai/api/v1/mcp/projects/{name}/tasks  # 获取项目任务
GET    /todo-for-ai/api/v1/mcp/tasks/{id}             # 获取任务详情
POST   /todo-for-ai/api/v1/mcp/tasks                  # 创建任务
POST   /todo-for-ai/api/v1/mcp/tasks/{id}/feedback    # 提交反馈
```

## 🏗️ 项目结构

```
todo-for-ai-api-server/
├── api/                    # API 端点
│   ├── auth.py            # 认证端点
│   ├── projects.py        # 项目管理
│   ├── tasks.py           # 任务管理
│   ├── mcp.py             # MCP 集成
│   ├── tokens.py          # 令牌管理
│   └── ...
├── core/                   # 核心模块
│   ├── config.py          # 配置
│   ├── auth.py            # 认证逻辑
│   ├── middleware.py      # 中间件
│   └── ...
├── models/                 # 数据库模型
│   ├── user.py            # 用户模型
│   ├── project.py         # 项目模型
│   ├── task.py            # 任务模型
│   └── ...
├── migrations/             # 数据库迁移
├── app.py                 # 应用入口点
├── requirements.txt       # Python 依赖
└── README.md             # 本文件
```

## 🧪 测试

### 手动测试

```bash
# 测试健康端点
curl http://localhost:50110/health

# 测试 API 文档
curl http://localhost:50110/todo-for-ai/api/v1/docs

# 测试认证
curl -X POST http://localhost:50110/todo-for-ai/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password"}'
```

### 运行测试

```bash
# 安装测试依赖
pip install pytest pytest-flask pytest-cov

# 运行测试
pytest

# 运行覆盖率测试
pytest --cov=api --cov=core --cov=models
```

## 🚀 部署

### Docker 部署

```bash
# 构建镜像
docker build -t todo-for-ai-api:latest .

# 运行容器
docker run -d --name todo-for-ai-api \
  -p 50110:50110 \
  -e DATABASE_URL="mysql+pymysql://user:pass@host:3306/db" \
  -e SECRET_KEY="your-secret-key" \
  todo-for-ai-api:latest
```

### 生产部署

1. **使用生产 WSGI 服务器**
```bash
gunicorn -w 4 -b 0.0.0.0:50110 app:app
```

2. **设置生产环境**
```bash
export FLASK_ENV=production
export DATABASE_URL="mysql+pymysql://user:pass@host:3306/db"
```

3. **配置反向代理** (推荐使用 nginx)

## 🔍 故障排除

### 常见问题

1. **数据库连接错误**
   - 检查 DATABASE_URL 格式
   - 确保 MySQL 服务器正在运行
   - 验证凭据和数据库是否存在

2. **OAuth 认证失败**
   - 检查 OAuth 客户端凭据
   - 验证回调 URL 是否匹配配置
   - 确保 OAuth 应用配置正确

3. **CORS 问题**
   - 检查 CORS_ORIGINS 环境变量
   - 验证前端 URL 是否包含在允许的来源中

### 日志

```bash
# 查看应用日志
tail -f logs/todo_for_ai.log

# 查看 Flask 调试日志 (开发环境)
export FLASK_ENV=development
python app.py
```

## 🤝 贡献

1. Fork 仓库
2. 创建功能分支
3. 进行更改
4. 添加测试
5. 提交 Pull Request

## 📄 许可证

MIT License

---

**🌟 准备集成了吗？** 访问 [https://todo4ai.org/](https://todo4ai.org/) 开始使用我们的 API 构建！
