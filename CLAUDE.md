# CLAUDE.md

Context for AI coding agents working in this repository.

## Project purpose

This repo provides two reusable AWS EKS AI capabilities in one codebase:

1. Proactive incident flow (automatic detection -> analysis -> Slack -> optional GitHub PR)
2. Interactive Slack bot for EKS troubleshooting (orchestrator + specialist + MCP tools)

## Current architecture

### Proactive path

EventBridge/CloudWatch/EKS events -> `sre_agent.proactive.handler` -> `ProactiveIncidentFlow` ->
`SREAgent` -> Slack update + DynamoDB persistence + optional GitHub fix PR.

### Interactive path

Slack mention -> `sre_agent.interactive.handler` -> `K8sOrchestratorAgent` ->
- non-K8s intent: early exit message
- K8s troubleshooting intent: route to `InteractiveEKSAgent` specialist ->
MCP-backed EKS tools (`interactive/mcp_tools.py`) -> thread reply.

Intent classification supports keyword fallback and optional Nova Micro via Bedrock.

## Important module map

```text
src/sre_agent/
  proactive/
    handler.py
    flow.py
  interactive/
    handler.py
    orchestrator.py
    strands_agent.py
    mcp_tools.py
  shared/
    config.py
    prompts.py

  # backward-compatible wrappers
  handler.py
  bot_handler.py
```

## Dependency strategy

- Single source of truth: `pyproject.toml`
- Install with editable mode:
  - runtime + dev: `pip install -e ".[dev]"`
  - optional strands extras: `pip install -e ".[strands]"`
- `requirements.txt` is intentionally removed to avoid drift.

## Deployment references

- SAM template: `infrastructure/template.yaml`
- Build/deploy helpers: `Makefile`
- Tests:
  - all: `pytest --no-cov`
  - orchestrator-focused: `pytest --no-cov tests/test_orchestrator.py`

## Agent implementation conventions

- Keep proactive and interactive concerns separated.
- Shared settings/prompts should live in `shared/`.
- Do not remove backward-compatible wrappers unless explicitly requested.
- Keep changes minimal and practical; avoid unnecessary abstractions.
