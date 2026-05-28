"""FastAPI app: tool endpoints for the OpenClaw agent + browser dashboard.

Single process owns:
- 7 tool HTTP endpoints under /api/tools/* (OpenClaw calls these)
- Dashboard at /dashboard (Tailwind+DaisyUI single-page UI)
- WebSocket /ws/live (pushes agent_events to connected dashboards)
- aiosqlite connection lifecycle (same DB as the existing orchestrator)
- Adapter registry + verifier registry + APScheduler (started on FastAPI lifespan)
"""
