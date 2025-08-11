# Todo for AI API Server

[中文版本](README_zh.md) | **English**

A RESTful API server for the Todo for AI task management system, built with Flask and designed specifically for AI assistant integration.

> 🚀 **Try it now**: Visit [https://todo4ai.org/](https://todo4ai.org/) to experience our product!

## ✨ Features

- 🔐 **Multi-Authentication**: JWT tokens and API tokens support
- 🌐 **OAuth Integration**: Google and GitHub OAuth login
- 📊 **Project Management**: Complete project lifecycle management
- ✅ **Task Management**: Advanced task tracking with status, priority, and assignments
- 🤖 **AI Integration**: MCP (Model Context Protocol) support for AI assistants
- 📝 **Context Rules**: Custom prompts and context management
- 🔒 **Security**: Token encryption, CORS protection, and secure session management
- 📈 **Dashboard**: Real-time statistics and analytics
- 🔍 **Search & Filter**: Advanced filtering and sorting capabilities
- 📱 **RESTful API**: Clean, well-documented REST endpoints

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- MySQL 5.7+ or 8.0+
- pip or conda

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/todo-for-ai/todo-for-ai-api-server.git
cd todo-for-ai-api-server
```

2. **Create virtual environment**
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Configure environment variables**
```bash
cp .env.example .env
# Edit .env with your configuration
```

5. **Initialize database**
```bash
python migrations/create_database.py
```

6. **Run the server**
```bash
python app.py
```

The API server will be available at `http://localhost:50110`

## 🔧 Configuration

### Environment Variables

Create a `.env` file with the following variables:

```bash
# Database Configuration
DATABASE_URL=mysql+pymysql://username:password@localhost:3306/todo_for_ai

# Flask Configuration
SECRET_KEY=your-secret-key-here
JWT_SECRET_KEY=your-jwt-secret-key-here
FLASK_ENV=development

# OAuth Configuration
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GITHUB_CLIENT_ID=your-github-client-id
GITHUB_CLIENT_SECRET=your-github-client-secret

# Server Configuration
HOST=127.0.0.1
PORT=50110

# CORS Configuration
CORS_ORIGINS=http://localhost:5173,http://localhost:50111

# Logging
LOG_LEVEL=INFO
LOG_FILE=app.log
```

### OAuth Setup

#### Google OAuth
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Google+ API
4. Create OAuth 2.0 credentials
5. Add authorized redirect URI: `http://localhost:50110/todo-for-ai/api/v1/auth/google/callback`

#### GitHub OAuth
1. Go to [GitHub Developer Settings](https://github.com/settings/developers)
2. Create a new OAuth App
3. Set Authorization callback URL: `http://localhost:50110/todo-for-ai/api/v1/auth/github/callback`

## 📚 API Documentation

### Base URL
```
http://localhost:50110/todo-for-ai/api/v1
```

### Authentication

The API supports two authentication methods:

1. **JWT Tokens** (for web applications)
2. **API Tokens** (for AI assistants and integrations)

#### Using JWT Tokens
```bash
# Login to get JWT token
curl -X POST http://localhost:50110/todo-for-ai/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "password"}'

# Use token in requests
curl -H "Authorization: Bearer <jwt_token>" \
  http://localhost:50110/todo-for-ai/api/v1/projects
```

#### Using API Tokens
```bash
# Create API token
curl -X POST http://localhost:50110/todo-for-ai/api/v1/api-tokens \
  -H "Authorization: Bearer <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "My API Token", "expires_in_days": 30}'

# Use API token in requests
curl -H "Authorization: Bearer <api_token>" \
  http://localhost:50110/todo-for-ai/api/v1/projects
```

### Core Endpoints

#### Health Check
```bash
GET /health
GET /todo-for-ai/api/v1/health
```

#### Authentication
```bash
POST /todo-for-ai/api/v1/auth/login
POST /todo-for-ai/api/v1/auth/logout
GET  /todo-for-ai/api/v1/auth/google
GET  /todo-for-ai/api/v1/auth/github
```

#### Projects
```bash
GET    /todo-for-ai/api/v1/projects          # List projects
POST   /todo-for-ai/api/v1/projects          # Create project
GET    /todo-for-ai/api/v1/projects/{id}     # Get project
PUT    /todo-for-ai/api/v1/projects/{id}     # Update project
DELETE /todo-for-ai/api/v1/projects/{id}     # Delete project
```

#### Tasks
```bash
GET    /todo-for-ai/api/v1/tasks             # List tasks
POST   /todo-for-ai/api/v1/tasks             # Create task
GET    /todo-for-ai/api/v1/tasks/{id}        # Get task
PUT    /todo-for-ai/api/v1/tasks/{id}        # Update task
DELETE /todo-for-ai/api/v1/tasks/{id}        # Delete task
POST   /todo-for-ai/api/v1/tasks/{id}/feedback # Submit feedback
```

#### MCP Integration
```bash
GET    /todo-for-ai/api/v1/mcp/projects/{name}/tasks  # Get project tasks
GET    /todo-for-ai/api/v1/mcp/tasks/{id}             # Get task details
POST   /todo-for-ai/api/v1/mcp/tasks                  # Create task
POST   /todo-for-ai/api/v1/mcp/tasks/{id}/feedback    # Submit feedback
```

## 🏗️ Project Structure

```
todo-for-ai-api-server/
├── api/                    # API endpoints
│   ├── auth.py            # Authentication endpoints
│   ├── projects.py        # Project management
│   ├── tasks.py           # Task management
│   ├── mcp.py             # MCP integration
│   ├── tokens.py          # Token management
│   └── ...
├── core/                   # Core modules
│   ├── config.py          # Configuration
│   ├── auth.py            # Authentication logic
│   ├── middleware.py      # Middleware
│   └── ...
├── models/                 # Database models
│   ├── user.py            # User model
│   ├── project.py         # Project model
│   ├── task.py            # Task model
│   └── ...
├── migrations/             # Database migrations
├── app.py                 # Application entry point
├── requirements.txt       # Python dependencies
└── README.md             # This file
```

## 🧪 Testing

### Manual Testing

```bash
# Test health endpoint
curl http://localhost:50110/health

# Test API documentation
curl http://localhost:50110/todo-for-ai/api/v1/docs

# Test authentication
curl -X POST http://localhost:50110/todo-for-ai/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password"}'
```

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-flask pytest-cov

# Run tests
pytest

# Run with coverage
pytest --cov=api --cov=core --cov=models
```

## 🚀 Deployment

### Docker Deployment

```bash
# Build image
docker build -t todo-for-ai-api:latest .

# Run container
docker run -d --name todo-for-ai-api \
  -p 50110:50110 \
  -e DATABASE_URL="mysql+pymysql://user:pass@host:3306/db" \
  -e SECRET_KEY="your-secret-key" \
  todo-for-ai-api:latest
```

### Production Deployment

1. **Use production WSGI server**
```bash
gunicorn -w 4 -b 0.0.0.0:50110 app:app
```

2. **Set production environment**
```bash
export FLASK_ENV=production
export DATABASE_URL="mysql+pymysql://user:pass@host:3306/db"
```

3. **Configure reverse proxy** (nginx recommended)

## 🔍 Troubleshooting

### Common Issues

1. **Database Connection Error**
   - Check DATABASE_URL format
   - Ensure MySQL server is running
   - Verify credentials and database exists

2. **OAuth Authentication Failed**
   - Check OAuth client credentials
   - Verify callback URLs match configuration
   - Ensure OAuth apps are properly configured

3. **CORS Issues**
   - Check CORS_ORIGINS environment variable
   - Verify frontend URL is included in allowed origins

### Logs

```bash
# View application logs
tail -f logs/todo_for_ai.log

# View Flask debug logs (development)
export FLASK_ENV=development
python app.py
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## 📄 License

MIT License

---

**🌟 Ready to integrate?** Visit [https://todo4ai.org/](https://todo4ai.org/) and start building with our API!
