"""Shared constants for tasks APIs."""

TASKS_LIST_CACHE_TTL_SECONDS = 15
tasks_list_fallback_cache = {}
MAX_ATTACHMENT_SIZE_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.txt', '.md', '.json', '.csv', '.pdf',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg',
    '.zip', '.tar', '.gz',
    '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.sql', '.yaml', '.yml'
}
