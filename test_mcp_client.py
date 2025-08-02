#!/usr/bin/env python3
"""
Test client for Todo for AI MCP Server - DEPRECATED

This script is deprecated as the Python MCP server has been removed.
Please use the npm package 'todo-for-ai-mcp' for MCP functionality.

Install with: npm install -g todo-for-ai-mcp
"""

import sys

def main():
    """Main function - shows deprecation notice"""
    print("❌ MCP Test Client is DEPRECATED")
    print("")
    print("The Python MCP server implementation has been removed.")
    print("Please use the npm package 'todo-for-ai-mcp' instead.")
    print("")
    print("Installation:")
    print("  npm install -g todo-for-ai-mcp")
    print("")
    print("Usage:")
    print("  todo-for-ai-mcp --help")
    print("")
    print("For testing, please refer to the npm package documentation.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
