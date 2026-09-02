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

MCP_TOOLS.extend([
    {
        "name": "list_my_tasks",
        "description": "List tasks relevant to the current API token user: created by them, owned by them, in their own projects, or assigned to them. This is the entry point for an external agent to discover work",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["todo", "in_progress", "review", "done", "cancelled"]
                    },
                    "description": "Filter tasks by status (default: todo, in_progress, review)",
                    "default": ["todo", "in_progress", "review"]
                },
                "project_id": {
                    "type": "integer",
                    "description": "Restrict to one project (optional)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max tasks returned (default 50, max 200)",
                    "default": 50
                }
            },
            "required": []
        }
    },
    {
        "name": "search_tasks",
        "description": "Search accessible tasks by keyword in title or content",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Keyword to search in task title/content"
                },
                "project_id": {
                    "type": "integer",
                    "description": "Restrict to one project (optional)"
                },
                "status": {
                    "type": "string",
                    "enum": ["todo", "in_progress", "review", "done", "cancelled"],
                    "description": "Filter by a single status (optional)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max tasks returned (default 50, max 200)",
                    "default": 50
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "update_task_status",
        "description": "Update a task's status (todo/in_progress/review/done/cancelled). Pass expected_revision to guard against concurrent edits; a mismatch is rejected",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task"
                },
                "status": {
                    "type": "string",
                    "enum": ["todo", "in_progress", "review", "done", "cancelled"],
                    "description": "The new status"
                },
                "expected_revision": {
                    "type": "integer",
                    "description": "Optimistic concurrency guard: reject the update if the task revision differs (optional)"
                }
            },
            "required": ["task_id", "status"]
        }
    },
    {
        "name": "report_progress",
        "description": "Append a progress log entry to a task (append-only task log). Use this to keep humans informed while working on a task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task"
                },
                "content": {
                    "type": "string",
                    "description": "Progress note in markdown (what was done, current state, next steps)"
                },
                "content_type": {
                    "type": "string",
                    "description": "Content type, default text/markdown",
                    "default": "text/markdown"
                }
            },
            "required": ["task_id", "content"]
        }
    },
    {
        "name": "request_approval",
        "description": "Ask the human to decide something about a task (e.g. destructive operation, budget, scope change). The request enters the workspace approval queue and workspace owner/admin can approve or reject it",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task this request is about"
                },
                "question": {
                    "type": "string",
                    "description": "What you are asking for and why (shown to the approver)"
                },
                "interaction_type": {
                    "type": "string",
                    "description": "Short type tag, e.g. human_approval / permission_request / budget_request (default: human_approval)",
                    "default": "human_approval"
                },
                "sensitivity_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "How sensitive the requested action is (default: medium)",
                    "default": "medium"
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional decision options to present to the approver"
                }
            },
            "required": ["task_id", "question"]
        }
    },
    {
        "name": "get_task_evidence",
        "description": "Get a task's Definition of Done (DoD) and verification evidence (test/build/lint results, linked pull requests) submitted by agents",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task"
                }
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "set_task_dod",
        "description": "Set a task's Definition of Done: machine-checkable acceptance criteria (test/build/lint/command) that an agent must satisfy with evidence before the task can be marked done",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The ID of the task"
                },
                "dod": {
                    "type": "array",
                    "description": "DoD criteria list; submit an empty array to clear",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["test", "build", "lint", "command", "pr", "manual"],
                                "description": "Criterion type"
                            },
                            "value": {
                                "type": "string",
                                "description": "The check itself, e.g. 'pytest -q'"
                            }
                        },
                        "required": ["type", "value"]
                    }
                }
            },
            "required": ["task_id", "dod"]
        }
    },
])
