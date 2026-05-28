"""MCP stdio server — thin proxy from MCP protocol to our FastAPI tool endpoints.
OpenClaw spawns this as a child process and talks to it over stdio.
Reads APP_BASE_URL + AGENT_TOOLS_TOKEN from env."""
