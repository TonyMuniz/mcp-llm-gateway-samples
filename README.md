# MCP and LLM gateway samples

Four small, self-contained Python projects around the Model Context Protocol (MCP) and LLM gateways.
Each folder is its own project with its own README, `pyproject.toml`, `uv.lock` and tests.

| Folder | Task | Tests |
|---|---|---|
| [`task1`](task1/) | Custom MCP server with strict validation and stdio transport handling | 137 checks |
| [`task2`](task2/) | MCP security gateway proxy (role-based tool authorization) | 22 |
| [`task3`](task3/) | LLM gateway streaming guardrail (real-time PII redaction) | 39 |
| [`task4`](task4/) | Rate-limiting and model fallback router (SQLite sliding window, failover) | 64 |

## Requirements

- Python 3.11 or newer (developed and tested on 3.15.2; each project pins 3.15 in `.python-version`)
- [uv](https://docs.astral.sh/uv/)

## Quick start

Run commands from inside the task's folder:

```bash
cd task1                      # or task2, task3, task4
uv sync                       # creates .venv from pyproject.toml / uv.lock
uv run python -m unittest -v  # runs that task's test suite
```

Each README lists the exact commands, the assignment requirements and where they are met, design
decisions, and known issues and limits.

## History

Each project was developed in its own repository; their histories were merged into this one, so
`git log -- task3` shows that project's commits.
