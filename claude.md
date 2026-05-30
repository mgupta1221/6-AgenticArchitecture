# Session 6 — Agentic Architecture

## What this project is

An agentic application with a strict orchestration loop (Agent6 loop) and five runtime components: Memory, Perception, Decision, Action, ArtifactStore. The full architectural specification is in `agent6_prompt.md` — that file is the source of truth for all design decisions, data contracts, and execution flow.

## Existing infrastructure (DO NOT recreate)

- **LLM Gateway V7**: `llm_gatewayV7/` — already built. Run with `cd llm_gatewayV7 && python main.py` (serves on http://localhost:8107). Use the client at `llm_gatewayV7/client.py` (`from llm_gatewayV7.client import LLM`). V7 is V3 plus a `POST /v1/embed` endpoint (Ollama default, Gemini fallback, 768-dim).
- **MCP Server**: `mcp_server.py` — already built. 9 tools: web_search, fetch_url, get_time, currency_convert, read_file, list_dir, create_file, update_file, edit_file. Stdio transport. Requires `.env` file and dependencies: `ddgs`, `crawl4ai`, `tavily`.

## What needs to be built

The Agent6 loop and its five components. Follow `agent6_prompt.md` exactly for:
- Pydantic models (MemoryItem, Goal, Observation, Observe, DecisionOutput, ToolCall, Artifact)
- Execution flow (steps 1-17)
- Gateway routing (Perception: `provider="g"`, Decision: `auto_route="decision"`, Memory: `auto_route="memory"`)
- Artifact management (in-memory vs artifact, 4096-byte threshold, artifactId propagation)
- Terminal output style (`[memory.read]`, `[perception]`, `[decision]`, `[action]`, etc.)
- Session 6 constraints (fixed goals, fail-fast, no compacting, single agent)

## Project structure

```
Project/
  agent6_prompt.md          # Architectural spec (read-only reference)
  claude.md                 # This file
  mcp_server.py             # MCP server (already built)
  llm_gatewayV7/            # LLM gateway (already built, V3 + embed endpoint)
    client.py               # Gateway client — import LLM from here
    main.py                 # Gateway server entry point
    ...
  agent6.py                 # TO BUILD: main entry point, Agent6 loop
  perception.py             # TO BUILD: Perception component
  decision.py               # TO BUILD: Decision component
  memory.py                 # TO BUILD: Memory component
  action.py                 # TO BUILD: Action component
  artifact_store.py         # TO BUILD: ArtifactStore component
  models.py                 # TO BUILD: all shared Pydantic models
  prompts.py                # TO BUILD: system prompts for Perception, Decision, Memory
  state/
    memory.json             # Persistent memory store (created at runtime)
    artifacts/              # Artifact files: .bin + .json per artifact
  .env                      # API keys (not committed)
```

## How to use the gateway client

```python
from llm_gatewayV7.client import LLM

llm = LLM()  # defaults to http://localhost:8107

# Perception — pinned to Gemini
result = llm.chat(
    prompt=perception_prompt,
    system=PERCEPTION_SYSTEM_PROMPT,
    provider="g",
    response_format=observation_json_schema,
    temperature=0.3,
)

# Decision — auto-routed
result = llm.chat(
    prompt=decision_prompt,
    system=DECISION_SYSTEM_PROMPT,
    auto_route="decision",
    tools=mcp_tool_list,
    temperature=0.3,
)

# Memory classification — auto-routed
result = llm.chat(
    prompt=memory_prompt,
    system=MEMORY_SYSTEM_PROMPT,
    auto_route="memory",
    response_format=memory_item_json_schema,
    temperature=0.2,
)
```

## How to connect to the MCP server

The MCP server uses stdio transport. Connect via the `mcp` Python SDK:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command="python",
    args=["mcp_server.py"],
)

async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(tool_name, arguments=args)
```

## Running the project

1. Start the gateway: `cd llm_gatewayV7 && python main.py`
2. Ensure `.env` has required API keys
3. Run the agent: `python agent6.py`

The agent starts, accepts a query, and runs the Agent6 loop until all goals are resolved.

## Key rules

- Read `agent6_prompt.md` before writing any component — it has all the contracts.
- Agent6 loop is plain Python, no LLM calls in the loop itself.
- Components never call each other directly — all routing through Agent6 loop.
- Use `response_format` for structured output from Perception and Memory.
- Artifact boundary: Perception sees handles only; Decision sees bytes only when Perception attaches them.
- Test with the two sample queries in `agent6_prompt.md` (Claude Shannon, Tokyo activities).
