You are building an agentic application with a strict orchestration loop and five runtime components:

1) Memory
2) Perception
3) Decision
4) Action
5) ArtifactStore

There is also one outer controller called Agent6 loop. The components do not call each other directly. All routing happens through Agent6 loop.

The loop must emit sequential component-level output in this order whenever applicable:
- memory
- perception
- decision
- action
- back to memory
- back to perception
- etc.

The loop continues until all goals are done or Decision returns a final answer.

==================================================
SESSION 6 CONSTRAINTS
==================================================

This is a Session 6 architecture. The following hard rules apply:

- Goals are fixed after the first Perception call. No adding, removing, or modifying goals mid-run.
- If any goal fails, the entire task fails. No partial success.
- Full history is passed every iteration — no compacting or summarization.
- Single agent only. No multi-agent orchestration.
- Each goal gets at most one tool call per iteration.

==================================================
STRICT EXECUTION FLOW
==================================================

For every user query:

1. Receive the query.
2. Load memory from state/memory.json on first read.
3. Call Memory.read(...) for retrieval.
4. Build the Observe packet:
   - query
   - memory hits
   - history (empty list on first iteration)
   - prior goals (None on first iteration)
   - any explicitly attached artifact bytes for the current goal only
5. Call Perception.observe(...) with the Observe packet.
6. Perception returns the current goal list with done flags and optional artifact attachments.
   On the first iteration, Perception creates the initial goal list from scratch.
   On subsequent iterations, Perception receives the previous goal list and updates done flags but does not add or remove goals.
7. Agent6 loop selects the next unfinished goal.
8. Call Decision.next_step(...) with:
   - selected goal
   - relevant history
   - tool list
   - optional attached artifact bytes only if Perception explicitly attached them
9. Decision returns either:
   - a final answer in plain text, or
   - exactly one tool call to MCP
10. If Decision returns a tool call, call Action.execute(...).
11. Action dispatches the MCP tool.
12. Tool output is evaluated for artifact promotion:
    - If output exceeds 4096 bytes: raw bytes are written into ArtifactStore and only the artifact handle plus descriptor is returned.
    - If output must persist across turns, is reused across workflows, requires iterative updates, or becomes a durable source of truth: promote to artifact regardless of size.
    - Otherwise (temporary, under 4 KB, actively being processed): return as short text in-memory.
13. Call Memory.record_outcome(...) with the tool call, result text, and optional artifact id.
    record_outcome is LLM-powered: it classifies the tool result and extracts facts, preferences, or observations to store as typed memory items.
    Routed via auto_route="memory".
14. Append a history event as a plain dict mirroring one of the typed shapes.
15. Re-run Perception with updated history and prior goals.
16. Repeat until all goals are done.
17. Return final answer.

Only Agent6 loop routes packets. No component talks to any other component directly.

==================================================
COMPONENT ROLES
==================================================

--------------------------------------------------
1) MEMORY
--------------------------------------------------

Memory is a typed service that stores:
- facts
- preferences
- tool outcomes
- scratchpad entries

Memory exposes:
- read(query, history, kinds=None, top_k=8)
- filter(kinds=..., goal_id=..., recent=N)
- relevant(query, kinds=..., top_k=5)
- remember(raw_text, source, run_id, goal_id)
- record_outcome(tool_call, result_text, artifact_id, ...)

Memory item kinds:
- fact
- preference
- tool_outcome
- scratchpad

Semantics:
- fact: durable observed truth
- preference: user-stated or inferred preference
- tool_outcome: one MCP dispatch result
- scratchpad: run-scoped working note for the current run only

Read methods:
- memory.read(query, history, kinds=None, top_k=8)
  - pure Python keyword overlap search across keywords plus descriptor tokens
  - no LLM
  - no embeddings, no vector similarity — keyword/substring matching only (Session 6 simplification)
  - returns ranked top-k
- memory.filter(kinds=..., goal_id=..., recent=N)
  - pure Python structured filter
  - no LLM
- memory.relevant(query, kinds=..., top_k=5)
  - LLM-scored relevance over a filtered candidate pool
  - used only when keyword recall is weak
  - one gateway call via auto_route="memory"

Write methods:
- memory.remember(raw_text, source, run_id, goal_id)
  - for ambiguous free-form content such as user input or observed statements
  - one classification call
  - routed via auto_route="memory"
  - pinned to Gemini
  - returns a typed item with kind, keywords, descriptor, and structured value extracted by the LLM
- memory.record_outcome(tool_call, result_text, artifact_id, ...)
  - for MCP dispatch results
  - LLM-powered: classifies the tool result and extracts facts, preferences, or observations
  - routed via auto_route="memory"
  - returns one or more typed MemoryItems with kind, keywords, descriptor, and structured value

All memory items live in:
- state/memory.json

The Agent6 loop loads this file on first read and writes it back after every mutation.

Memory schema:

```python
class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict
    artifact_id: str | None
    source: str
    run_id: str
    goal_id: str | None
    confidence: float
    created_at: datetime
```

--------------------------------------------------
2) PERCEPTION
--------------------------------------------------

Perception is the goal planner and tracker.

Inputs (via the Observe packet):

- query
- memory hits
- history (empty list on first iteration)
- prior goals (None on first iteration)
- optional artifact attachments

Input schema:

```python
class Observe(BaseModel):
    query: str
    memory_hits: list[MemoryItem]
    history: list[dict]
    prior_goals: list[Goal] | None  # None on first iteration
```

On the first iteration, `prior_goals` is `None`. History is empty. Perception creates the initial goal list from scratch. On subsequent iterations, Perception receives the previous goal list and updates done flags.

Output:

- current goal list with done flags
- optional artifact attachments

Behavior:

- called on every iteration
- one LLM call per iteration
- routed via auto_route="perception"
- pinned to Gemini in this session

Perception reads the query, memory hits, and history, and emits the current goal list with done flags and optional artifact attachments.

Perception does not execute tools.
Perception does not directly read raw artifact bytes unless the Agent6 loop explicitly attaches them for the current goal.

artifactId propagation: If input content already exists as an artifact, the Observe packet must include the artifactId. If Perception generates durable output (e.g., attaching an artifact to a goal), the output must return the artifactId via the `attach_artifact_id` field on the goal.

Perception schema:

```python
class Goal(BaseModel):
    id: str
    text: str
    done: bool
    attach_artifact_id: str | None

class Observation(BaseModel):
    goals: list[Goal]
```

Perception responsibilities:

- break the task into bounded goals (first iteration only)
- mark goals done or pending using the available context
- decide whether an artifact handle should be attached for a goal
- keep the goal list coherent across iterations

Perception maintains the goal list logically. Agent6 loop owns orchestration and state passing.

--------------------------------------------------
3) DECISION
--------------------------------------------------

Decision picks the next action for one bounded goal.

Inputs:

- one selected goal
- relevant history
- available tools (included in the system prompt)
- any artifact bytes explicitly attached by Perception for the current goal

Outputs:

- either a final answer in plain text
- or exactly one MCP tool call

If Decision returns anything other than exactly one ToolCall or one FinalAnswer, the loop MUST treat it as an error.

Behavior:

- called once per iteration when there is an unfinished goal
- one LLM call per iteration
- routed via auto_route="decision"

Decision does not mutate memory.
Decision does not execute tools.
Decision only chooses between final answer and one tool call.

artifactId propagation: Decision must consume artifacts by reference using artifactId rather than copying full content. Any decision output that creates or modifies durable state must emit the corresponding artifactId.

Decision schema:

```python
class ToolCall(BaseModel):
    name: str
    arguments: dict

class DecisionOutput(BaseModel):
    answer: str | None
    tool_call: ToolCall | None
```

Exactly one of answer or tool_call is populated.

--------------------------------------------------
4) ACTION
--------------------------------------------------

Action dispatches the selected MCP tool.

Inputs:

- tool call from Decision

Outputs:

- descriptor string
- optional artifact id

Behavior:

- only runs when Decision returns a tool_call
- pure dispatch
- no LLM

Action invokes the tool and returns:

- a short descriptor
- optionally an artifact id if the result exceeds 4096 bytes or meets any other artifact promotion criteria (persistence, reuse, iterative updates, source of truth)

artifactId propagation: Actions operating on durable content must accept artifactId as input. Any persisted action result must return or update the associated artifactId.

Action return type:

```python
tuple[str, str | None]
```

Meaning:

- descriptor
- optional artifact id

--------------------------------------------------
5) ARTIFACT STORE — the parallel store for raw bytes
--------------------------------------------------

When a tool produces a payload larger than a few kilobytes, the bytes are written to a separate content-addressable store. Memory holds only the handle.

Why this matters: A typical fetched web page is 100 KB or more (though lesser after cleanup). If those bytes lived inside MemoryItem.value, every subsequent Memory.read would either return them (bloating Decision's context window) or excerpt them (forcing the loop to maintain a second piece of state about which excerpt to use). The artifact store sidesteps the choice by holding the bytes separately and giving Memory a handle.

Store interface:

```python
INLINE_BUDGET = 4096  # bytes

class ArtifactStore:
    def put(self, blob: bytes, *,
            content_type: str, source: str, descriptor: str) -> str: ...
    def get_bytes(self, artifact_id: str) -> bytes: ...
    def get_meta(self, artifact_id: str) -> Artifact: ...
    def exists(self, artifact_id: str) -> bool: ...
```

Handles are short strings of the form `art:<sha256-prefix>`.

Storage is two files per artifact under `state/artifacts/`:
- a `.bin` with the raw bytes
- a `.json` with the metadata

The store is content-addressable; identical fetches deduplicate. The store has no eviction policy in S6.

Artifact schema:

```python
class Artifact(BaseModel):
    id: str
    content_type: str
    size_bytes: int
    source: str
    descriptor: str
```

Strict architectural boundary:

```
            ┌──────────────────────────────────────────────────────┐
            │                                                      │
  Memory ◄──┤ holds the handle string ("art:abc...") inside        │
            │ MemoryItem.artifact_id                               │
            │                                                      │
  Perception ◄ sees the handle in MEMORY HITS, never the bytes     │
            │                                                      │
  Decision ◄  sees the bytes only when Perception attaches them    │
            │ to the prompt for the current goal                   │
            │                                                      │
  Action  ◄── produces bytes (writes them via ArtifactStore.put)   │
            │                                                      │
            └──────────────────────────────────────────────────────┘
```

Enforcement: The boundary is enforced by the Agent6 loop. Perception's output includes an optional `attach_artifact_id` field on each goal. When the next unfinished goal carries such a field, the loop calls `ArtifactStore.get_bytes(...)` and passes the result into Decision's prompt under an `ATTACHED ARTIFACTS:` section. Decision sees the section as part of its context window.

Cost rationale: A Decision call against a 4 KB context costs a fraction of one against a 200 KB context. Decision should only pay the larger cost when the work it is doing on this turn requires the bytes.

==================================================
ARTIFACT MANAGEMENT RULE
==================================================

Manage content in two modes: in-memory or artifact.

In-memory mode:
- Use when the content is temporary, actively being processed, and smaller than 4 KB.
- In-memory content flows directly between stages without creating a durable artifact.
- Keep only the current working state and avoid duplication across stages.

Create an artifact when:
- content exceeds 4 KB, OR
- the content must persist across turns, OR
- the content is reused across workflows, OR
- the content requires iterative updates, OR
- the content becomes a durable source of truth.

--------------------------------------------------
artifactId propagation across pipeline stages
--------------------------------------------------

When artifacts are used, the artifactId must be explicitly propagated through all workflow stages:

Perception stage:
- If input content already exists as an artifact, the perception input must include the artifactId.
- If perception generates durable output, the output must return an artifactId.

Decision stage:
- Decisions must consume artifacts by reference using artifactId rather than copying full content whenever possible.
- Any decision output that creates or modifies durable state must emit the corresponding artifactId.

Action stage:
- Actions operating on durable content must accept artifactId as input.
- Any persisted action result must return or update the associated artifactId.

General propagation rule:
- artifactId must travel with durable content across perception -> decision -> action boundaries.
- For in-memory content under 4 KB, pass the content directly without creating or propagating an artifactId.
- Promote in-memory content to an artifact immediately once persistence, reuse, or size requirements apply.

==================================================
GATEWAY RULES
==================================================

The gateway is the LLM Gateway V3 (folder: LLM_gatewayV3 in this directory). Every LLM call in the four roles routes through it at http://localhost:8101. The gateway is the same substrate Session 5 introduced, with the V3 additions described in Session 5's gateway README.

--------------------------------------------------
auto_route and the router pool
--------------------------------------------------

When a chat request carries `auto_route="perception"`, `auto_route="memory"`, or `auto_route="decision"`, the gateway runs a small classifier LLM (the router pool) over a bounded envelope containing only the token count and an 800-character sample of the prompt. The classifier returns one of three tier labels:

- TINY — lands on small fast workers
- LARGE — lands on long-context workers such as Gemini 3.1 flash-lite
- HUGE — returns 503 with a clear hint to chunk the input

The gateway maps the tier to a worker failover order and dispatches the actual call.

--------------------------------------------------
Separation-of-concerns wall
--------------------------------------------------

The router pool never sees the worker's prompt, system, tools, schema, or earlier turns. It receives only `{token_count, sample}`. The separation is enforced in code: the request envelope sent to the router LLM physically carries only the token count and the 800-character sample. The router cannot leak agentic context into routing decisions because the agentic context never reaches it.

--------------------------------------------------
Provider override
--------------------------------------------------

A caller can specify `provider="g"` (or any other shortcut) explicitly. The router is skipped entirely and the named provider becomes the first worker.

This session uses the override on every Perception call to send Perception to Gemini. The reason is empirical: the TINY-tier worker that the router selected was too small to reliably follow Perception's multi-step procedure. The override is the gateway feature designed for exactly this case.

--------------------------------------------------
Structured output via response_format
--------------------------------------------------

Perception and Memory both use this feature. The gateway translates the Pydantic JSON Schema into the per-provider response_format payload, applies the necessary cleaning for each provider, and validates the parsed output server-side. Callers receive a parsed dict already validated against the schema.

--------------------------------------------------
Routing summary
--------------------------------------------------

- Perception calls: `provider="g"` override — pinned to Gemini for reliability, router skipped.
- Memory classification and record_outcome calls: `auto_route="memory"` — pinned to Gemini for reliability via provider override.
- Memory.relevant calls: `auto_route="memory"` — only memory read path that uses an LLM, and only when keyword recall is weak.
- Decision calls: `auto_route="decision"` — router pool picks a worker based on the size and structure of the prompt for each call.

Perception and the Memory classifier both pin to Gemini for reliability. Decision uses the router pool to pick a worker dynamically.

==================================================
MCP SERVER
==================================================

The MCP server for Session 6 (`mcp_server.py`) exposes nine tools:

1. `web_search`
2. `fetch_url`
3. `get_time`
4. `currency_convert`
5. `read_file`
6. `list_dir`
7. `create_file`
8. `update_file`
9. `edit_file`

The full inventory and contracts are documented in the server file itself. Decision sees these nine tools as a tool list and picks one when external work is required.

Setup requirements:
- A `.env` file must be present with the necessary API keys.
- Python dependencies: `ddgs`, `crawl4ai`, `tavily`.

==================================================
HISTORY
==================================================

The Agent6 loop accumulates history as a list of plain dicts.

Every event in history must mirror one of the typed shapes above.

History records:

- query received
- memory hits found
- goals emitted
- tool called
- artifact created
- memory updated
- goal marked done
- final answer returned

Full history is passed every iteration — no compacting or summarization (Session 6 constraint).

==================================================
CONTROL RULES
==================================================

- Agent6 loop is a plain Python program, not an LLM. It contains NO LLM calls. It is a while loop that orchestrates calls to Perception (LLM), Decision (LLM), record_outcome (LLM), and Action (no LLM). All intelligence is in those components, never in the loop itself.
- The loop owns control flow and routing.
- Components never directly call each other.
- Every packet moves only through Agent6 loop.
- Perception owns goal interpretation.
- Decision owns next-step choice.
- Action owns dispatch.
- Memory owns typed recall and durable record updates.
- ArtifactStore owns raw byte blobs (file-backed under state/artifacts/, content-addressable, no eviction in S6).
- Tool output exceeding 4096 bytes must be stored as an artifact. Content under 4 KB that requires persistence, reuse, iterative updates, or serves as a durable source of truth must also be promoted to artifact.
- For in-memory content under 4 KB, pass the content directly without creating or propagating an artifactId.
- artifactId must travel with durable content across perception -> decision -> action boundaries.
- Memory updates after tool execution use record_outcome which is LLM-powered to classify and extract facts/preferences.
- Scratchpad entries are run-scoped and used only for intermediate planner state.
- If the goal does not require bytes, do not attach artifact bytes to Decision.
- If the goal requires bytes, Perception must explicitly attach the artifact handle, and Agent6 loop may pass the bytes onward.

==================================================
REQUIRED TERMINAL OUTPUT STYLE
==================================================

The system should log sequentially by component for each iteration.

Use this pattern:

```
[memory.read]
[memory.relevant]       only if used
[perception]
[decision]
[action]
[memory.record_outcome]
[attach]
[done]
FINAL
```

When an artifact is created:

- action must print the artifact handle
- preview may be shown
- Perception may later attach that artifact handle
- Decision may then consume the attached bytes

If multiple goals exist, the logs must show the goal list in Perception output and the selected action in Decision output for each iteration.

==================================================
PYDANTIC OUTPUT CONTRACTS
==================================================

Memory.read returns:

```python
list[MemoryItem]
```

Perception.observe returns:

```python
Observation
```

Perception.observe accepts:

```python
Observe
```

Decision.next_step returns:

```python
DecisionOutput
```

Action.execute returns:

```python
tuple[str, str | None]
```

The history that the loop accumulates is a list of plain dicts, but every event should mirror these typed shapes.

==================================================
SAMPLE OUTPUTS
==================================================

These are canonical examples of the expected sequential flow and logging style.

Test A — Claude Shannon (artifact attach)

QUERY: Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.

```
press ENTER to continue

run bcbd84af
— query: Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.

Processing request of type ListToolsRequest

mcpl loaded 9 tools: ['web_search', 'fetch_url', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

iter 1
[memory.read] 1 hits
[perception] o g:f368ff6a — Fetch https://en.wikipedia.org/wiki/Claude_Shannon
[perception] o g:cabb0d29 — Extract Claude Shannon's birth date, death date, and three key contributions to information theory
[decision] TOOL_CALL: "https://en.wikipedia.org/wiki/Claude_Shannon"
[5/16/26 INFO Processing request of type CallToolRequest
server.py: 727

[INIT] Crawl4AI 0.8.6
[FETCH] https://en.wikipedia.org/wiki/Claude_Shannon
[SCRAPE] https://en.wikipedia.org/wiki/Claude_Shannon
[COMPLETE] https://en.wikipedia.org/wiki/Claude_Shannon
[action] -> [artifact art:96ff2446efcb6b4c, 263352 bytes] preview: {"status": 200, "content_type": "text/markdown", "length_bytes": 257240, "text": " [Jump to content] (https://en.wikipedia.org/wiki/Claude_Shannon"
[memory.record_outcome]
action
"text": " [Jump to content] (https://en.wikipedia.org/wiki/Claude_Shannon

iter 2
[memory.read] 2 hits
[perception] g:f368ff6a — Fetch https://en.wikipedia.org/wiki/Claude_Shannon
[perception] o g:cabb0d29 — Extract Claude Shannon's birth date, death date, and three key contributions to information theory attach — art:96ff2446efcb6b4c
[attach] art:96ff2446efcb6b4c (263352 bytes)
[decision]: ANSWER: Claude Shannon was born on April 30, 1916, and passed away on February 24, 2001.

He is widely recognized as the "father of information theory," and three of his key contributions to the field include.

iter 3
[memory.read] 2 hits
[perception] g:f368ff6a — Fetch https://en.wikipedia.org/wiki/Claude_Shannon
[perception] g:cabb0d29 — Extract Claude Shannon's birth date, death date, and three key contributions to information theory
[done] all 2 goals satisfied

FINAL

Claude Shannon was born on April 30, 1916, and passed away on February 24, 2001.
He is widely recognized as the "father of information theory," and three of his key contributions to the field include:

Mathematical Theory of Communication: In his seminal 1948 paper, he established the fundamental concepts of information theory, including informational entropy and the channel capacity theorem.
Quantification of Information: He introduced the bit (binary digit) as the basic unit of information, providing a mathematical framework to measure information content and transmission efficiency.
Error Correction and Data Compression: His work laid the theoretical foundation for modern digital communication by demonstrating how data could be compressed and transmitted reliably over noisy channels through error-correcting codes.
```

===========================================================

Test B — Tokyo activities + weather (multi-goal)

QUERY: Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast there and tell me which is most appropriate.

```
--press ENTER to continue--

query: Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast there and tell me which one is most appropriate.
Processing request of type ListToolsRequest

[mcp] loaded 9 tools: ['web_search', 'fetch_url', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

iter 1
[memory.read] 1 hits
[perception] o g:da584310 — Search for 3 family-friendly things to do in Tokyo for this weekend.
[perception] o g:8e76734e — Check the weather forecast for Saturday in Tokyo.
[perception] o g:8369b0b1 — Evaluate which of the 3 activities is most appropriate based on the weather forecast.
[decision]: TOOL_CALL: "weather forecast Tokyo Saturday November 30 2024"
Processing request of type CallToolRequest
server.py: 727
response: https://en.wikipedia.org/w/api.php?action=opensearch&profile=fuzzy&limit=1&search=weat
info 200
response: https://grokipedia.com/api/typeahead?query=weather+forecast+Tokyo+Saturday+November+30
response: https://search.yahoo.com/search?q=weather+forecast+Tokyo+Saturday+November+30
[action]: {"title": "November 30 Weather in Tokyo: Detailed Weather Forecast for November 30...", "snippet": "Detailed Weather Fo..."}
[memory.record_outcome]

iter 2
[memory.read] 3 hits
[perception] g:da584310 — Search for 3 family-friendly things to do in Tokyo for this weekend.
[perception] g:8e76734e — Check the weather forecast for Saturday in Tokyo.
[perception] g:8369b0b1 — Evaluate which of the 3 activities is most appropriate based on the weather forecast.
[decision] TOOL_CALL: "https://www.timeout.com/tokyo/things-to-do/things-to-do-in-tokyo-this-weekend-november-29-30"
'05/10/26 INFO Processing request of type CallToolRequest
[INIT] CRAWL4AI 0.8.6
[FETCH] https://timeout.com/tokyo/things-to-do-in-tokyo-this-weekend-november-29-30
[SCRAPE] https://timeout.com/tokyo/things-to-do-in-tokyo-this-weekend-november-29-30
[COMPLETE] https://timeout.com/tokyo/things-to-do-in-tokyo-this-weekend-november-29-30

[action] -> [artifact art:1a26bdbec30451f6, 22320 bytes] preview: {"status": 404, "content_type": "text/markdown", "length_bytes": 22019, "text": " [Go to the content] (https://www.timeout.com/tokyo/things
[memory.record_outcome]

iter 3
[memory.read] 4 hits
[perception] g:da584310 — Search for 3 family-friendly things to do in Tokyo for this weekend.
[perception] g:8e76734e — Check the weather forecast for Saturday in Tokyo.
[perception] o g:8369b0b1 — Evaluate which of the 3 activities is most appropriate based on the weather forecast.
[decision]: ANSWER: Based on the weather and activities found...

FINAL
...
```
