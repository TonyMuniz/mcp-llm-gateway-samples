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

- Python 3.11 or newer (developed and tested on 3.15.2; each project pins 3.15 in `.python-version`,
  and uv downloads that version automatically if it is not installed)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Nothing else: every task runs locally against the mock servers included in its folder. No API keys,
  accounts or external services are needed.

## Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/TonyMuniz/mcp-llm-gateway-samples.git
   cd mcp-llm-gateway-samples
   ```

2. Install uv if you do not have it:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh                       # Linux, macOS
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
   ```

3. For each task, create its environment and run its tests from inside the task's folder:

   ```bash
   cd task1                      # or task2, task3, task4
   uv sync                       # creates .venv from pyproject.toml / uv.lock
   uv run python -m unittest -v  # runs that task's test suite
   ```

Each task's README then shows how to start the server or gateway (and its mocks) by hand, and covers
the assignment requirements and where they are met, design decisions, and known issues and limits.

Development and testing were done on Linux (Debian 13, arm64). The commands are the same on macOS
and Windows, but those platforms were not tested.

## History

Each project was developed in its own repository; their histories were merged into this one, so
`git log -- task3` shows that project's commits.
