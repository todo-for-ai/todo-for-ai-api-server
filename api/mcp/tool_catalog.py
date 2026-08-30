MCP_TOOLS = [
    {
        "name": "get_project_tasks_by_name",
        "description": "Get all pending tasks for a project by project name, sorted by creation time",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "The name of the project to get tasks for"
                },
                "status_filter": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["todo", "in_progress", "review"]
                    },
                    "description": "Filter tasks by status (default: todo, in_progress, review)",
                    "default": ["todo", "in_progress", "review"]
                }
            },
            "required": ["project_name"]
        }
    },
    {
        "name": "get_task_by_id",
        "description": "Get detailed task information by task ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task to retrieve"
                }
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "create_task",
        "description": "Create a new task in the specified project",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "The ID of the project to create the task in"
                },
                "title": {
                    "type": "string",
                    "description": "The title of the task"
                },
                "content": {
                    "type": "string",
                    "description": "The detailed content/description of the task"
                },
                "status": {
                    "type": "string",
                    "enum": ["todo", "in_progress", "review", "done", "cancelled"],
                    "description": "The initial status of the task (default: todo)",
                    "default": "todo"
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "description": "The priority of the task (default: medium)",
                    "default": "medium"
                },
                "assignee": {
                    "type": "string",
                    "description": "The person assigned to this task (optional)"
                },
                "due_date": {
                    "type": "string",
                    "description": "The due date in YYYY-MM-DD format (optional)"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags associated with the task (optional)"
                },
                "related_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files related to this task (optional)"
                },
                "is_ai_task": {
                    "type": "boolean",
                    "description": "Whether this task was created by AI (default: true)",
                    "default": True
                },
                "ai_identifier": {
                    "type": "string",
                    "description": "Identifier of the AI creating the task (optional)"
                }
            },
            "required": ["project_id", "title"]
        }
    },
    {
        "name": "get_project_info",
        "description": "Get detailed project information including statistics and configuration. Provide either project_id or project_name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "The ID of the project to retrieve (optional if project_name is provided)"
                },
                "project_name": {
                    "type": "string",
                    "description": "The name of the project to retrieve (optional if project_id is provided)"
                }
            },
            "required": []
        }
    },
    {
        "name": "list_user_projects",
        "description": "List all projects that the current user has access to, with proper permission checking",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["active", "archived", "all"],
                    "description": "Filter projects by status (default: active)",
                    "default": "active"
                },
                "include_stats": {
                    "type": "boolean",
                    "description": "Whether to include project statistics (default: false)",
                    "default": False
                }
            },
            "required": []
        }
    },
    {
        "name": "submit_task_feedback",
        "description": "Submit feedback for a completed or in-progress task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task to provide feedback for"
                },
                "project_name": {
                    "type": "string",
                    "description": "The name of the project this task belongs to"
                },
                "feedback_content": {
                    "type": "string",
                    "description": "The feedback content describing what was done"
                },
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "review", "done", "cancelled"],
                    "description": "The new status of the task after feedback"
                },
                "ai_identifier": {
                    "type": "string",
                    "description": "Identifier of the AI providing feedback (optional)"
                }
            },
            "required": ["task_id", "project_name", "feedback_content", "status"]
        }
    }
]
