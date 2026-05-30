# Agentic Architecture (S6)

An agentic application built around a strict orchestration loop with five runtime components: **Memory**, **Perception**, **Decision**, **Action**, and **ArtifactStore**. The agent accepts a natural-language query, decomposes it into bounded goals, executes MCP tool calls to gather information, and synthesizes a final answer — all routed through an intelligent LLM gateway that distributes calls across multiple providers.

**Key upgrade in this version:** Memory now uses **FAISS vector search** with 768-dim embeddings for semantic retrieval, falling back to keyword overlap when embeddings are unavailable. Document indexing and knowledge search tools enable a full **RAG pipeline** over local files and fetched content.

---

## Demo

https://youtu.be/6I36ApvAGbo

---

## Project Structure

```
Project/
├── agent6.py              # Main orchestration loop (plain Python, no LLM calls)
├── perception.py          # Goal planner & tracker — breaks query into goals, tracks done flags
├── decision.py            # Next-step picker — returns one tool call or a final answer
├── action.py              # Pure MCP tool dispatch, no LLM — handles artifact promotion
├── memory.py              # Typed recall with hybrid vector + keyword search, FAISS index
├── artifact_store.py      # Content-addressable blob store for large outputs (>4 KB)
├── models.py              # All shared Pydantic models (Goal, Observation, DecisionOutput, etc.)
├── mcp_server.py          # MCP server with 11 tools (web search, fetch, file I/O, time, currency, index, search)
├── requirements.txt       # Python dependencies
├── agent6_prompt.md       # Full architectural specification (read-only reference)
├── .env                   # API keys (not committed)
├── llm_gatewayV7/         # LLM Gateway V7 (separate service)
│   ├── main.py            # FastAPI server — routing, tier classification, failover, embedding
│   ├── providers.py       # Adapters for 7 LLM providers (Gemini, Groq, NVIDIA, etc.)
│   ├── embedders.py       # Embedding providers (Ollama + Gemini fallback, 768-dim)
│   ├── router.py          # Router pool + worker pool state management
│   ├── schemas.py         # Gateway request/response Pydantic models
│   ├── client.py          # Python client SDK (import LLM from here)
│   ├── db.py              # Call logging (SQLite)
│   ├── cache.py           # Gemini explicit caching
│   └── README.md          # Full gateway documentation
├── state/                 # Runtime state (created automatically)
│   ├── memory.json        # Persisted memory items across runs (includes embeddings)
│   ├── index.faiss        # FAISS binary index (IndexFlatIP, 768-dim)
│   ├── index_ids.json     # Maps FAISS row index → MemoryItem ID
│   └── artifacts/         # .bin (raw bytes) + .json (metadata) per artifact
└── sandbox/               # Sandboxed directory for file tools
```

---

## Execution Flow

### How the Agent6 Loop Works

The loop is **plain Python** — it contains zero LLM calls. All intelligence lives in Perception (LLM), Decision (LLM), Memory.record_outcome (LLM), and Action (no LLM).

```
                          ┌─────────────────────────────────┐
                          │         User Query               │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │     memory.remember(query)       │
                          │  Classify query into memory item │
                          └────────────┬────────────────────┘
                                       │
                ┌──────────────────────►│
                │                      ▼
                │         ┌─────────────────────────────────┐
                │         │       memory.read(query)         │
                │         │  FAISS vector search (primary)   │
                │         │  Keyword fallback (if no embeds) │
                │         └────────────┬────────────────────┘
                │                      │
                │                      ▼
                │         ┌─────────────────────────────────┐
                │         │      Perception.observe()        │
                │         │  Create goals (iter 1) or        │
                │         │  update done flags (iter 2+)     │
                │         │  Optionally attach artifact ID   │
                │         └────────────┬────────────────────┘
                │                      │
                │                      ▼
                │                ┌───────────┐
                │                │ All goals  │──── YES ───► FINAL ANSWER
                │                │   done?    │
                │                └─────┬─────┘
                │                      │ NO
                │                      ▼
                │         ┌─────────────────────────────────┐
                │         │  Select next unfinished goal     │
                │         │  Attach artifact bytes if needed │
                │         └────────────┬────────────────────┘
                │                      │
                │                      ▼
                │         ┌─────────────────────────────────┐
                │         │     Decision.next_step()         │
                │         │  Returns ONE of:                 │
                │         │    • Final answer (plain text)   │
                │         │    • One MCP tool call            │
                │         └────────────┬────────────────────┘
                │                      │
                │              ┌───────┴───────┐
                │              │               │
                │           ANSWER         TOOL_CALL
                │              │               │
                │              ▼               ▼
                │     ┌──────────────┐  ┌─────────────────────┐
                │     │ Append to    │  │  Action.execute()    │
                │     │ history      │  │  Dispatch MCP tool   │
                │     │ (kind:answer)│  │  Promote to artifact │
                │     └──────┬───────┘  │  if output > 4 KB   │
                │            │          └──────────┬──────────┘
                │            │                     │
                │            │                     ▼
                │            │          ┌─────────────────────┐
                │            │          │ memory.record_outcome│
                │            │          │ LLM classifies result│
                │            │          │ Embed + FAISS index  │
                │            │          └──────────┬──────────┘
                │            │                     │
                │            │                     ▼
                │            │          ┌─────────────────────┐
                │            │          │ Append to history    │
                │            │          │ (kind: action)       │
                │            │          └──────────┬──────────┘
                │            │                     │
                └────────────┴─────────────────────┘
                         (next iteration)
```

### Step-by-Step Walkthrough

1. **Query received** — the user provides a natural-language question.
2. **`memory.remember(query)`** — classifies the query via LLM (Gemini) and stores it as a typed memory item so future runs can recall context. Scratchpad items skip embedding.
3. **MCP session opened** — connects to `mcp_server.py` via stdio, loads the 11 available tools.
4. **Iteration loop begins** (max 15 iterations):
   - **`memory.read()`** — **hybrid retrieval**: first embeds the query via gateway `/v1/embed` (768-dim) and runs FAISS cosine-similarity search across all stored embeddings. If vector search returns results, uses those. Falls back to pure Python keyword-overlap search if embedding fails or FAISS has no entries. Returns top-8 hits.
   - **`Perception.observe()`** — LLM call (Gemini). On the first iteration, creates the initial goal list. On subsequent iterations, reviews history and updates `done` flags. May set `attach_artifact_id` on a goal that needs raw artifact bytes.
   - **Completion check** — if all goals are `done` and an answer exists in history, the loop breaks.
   - **Goal selection** — picks the first unfinished goal.
   - **Artifact attachment** — if Perception set `attach_artifact_id`, the loop loads the raw bytes from `ArtifactStore` and passes them to Decision.
   - **`Decision.next_step()`** — LLM call (auto-routed). Receives the goal, memory hits, attached artifacts, history, and tool list. Returns either a final answer or exactly one tool call. Aware of `index_document` and `search_knowledge` for RAG workflows.
   - **If answer** — appended to history as `kind: "answer"`. Loop continues; next iteration Perception will mark the goal done.
   - **If tool call** — `Action.execute()` dispatches the MCP tool. If output exceeds 4096 bytes, it is stored in `ArtifactStore` and only a descriptor + handle are returned. `memory.record_outcome()` then classifies the result via LLM, generates embeddings, stores items in memory.json, and appends vectors to the FAISS index.
5. **Loop exits** — `final_answer_from(history)` retrieves the last `kind: "answer"` event.

### Memory Retrieval: Vector Search + Keyword Fallback

```
Query: "family activities in Tokyo"
         │
         ▼
  ┌──────────────────────────┐
  │  Embed query (768-dim)   │ ◄── gateway POST /v1/embed
  │  task_type: retrieval_   │     Ollama default, Gemini fallback
  │  query                   │
  └────────────┬─────────────┘
               │
               ▼
  ┌──────────────────────────┐
  │  FAISS IndexFlatIP       │ ◄── state/index.faiss
  │  L2-normalize + search   │     cosine similarity via inner product
  │  top_k * 2 candidates    │
  └────────────┬─────────────┘
               │
        ┌──────┴──────┐
    results?      no results / embed failed
        │                  │
        ▼                  ▼
  Return vector       Keyword fallback:
  search results      tokenize query +
  (semantic)          last 5 history,
                      score by overlap,
                      return top-k
```

### Document Indexing (RAG Pipeline)

Two new MCP tools enable a **Retrieval-Augmented Generation** workflow:

| Tool | Purpose |
|------|---------|
| `index_document(path)` | Chunks a sandbox file or artifact (400-word sliding window, 80-word overlap), writes each chunk as a `fact` MemoryItem with a 768-dim embedding into memory.json + FAISS index |
| `search_knowledge(query, k)` | Vector search over indexed `fact` chunks, returns top-k ranked results with provenance and chunk previews |

**Workflow:**
1. User says "make this file searchable" → Decision calls `index_document`
2. File is chunked → each chunk embedded → stored in FAISS + memory.json
3. Later: user asks a question → Decision calls `search_knowledge` → semantic search finds relevant chunks → Decision synthesizes the answer

### Artifact Boundary

```
Memory      ◄── holds the handle string ("art:abc...") inside MemoryItem.artifact_id
                 + 768-dim embedding vector for semantic search
Perception  ◄── sees the handle in memory hits, never the raw bytes
Decision    ◄── sees raw bytes ONLY when Perception attaches them to the current goal
Action      ◄── produces bytes (writes them via ArtifactStore.put)
```

A typical fetched web page is 100 KB+. Without the artifact store, those bytes would bloat every subsequent LLM call. The store sidesteps this by holding bytes separately and giving Memory a handle. Decision only pays the large-context cost when its current goal actually needs the bytes.

---

## Technology & Libraries

### Core Stack

| Technology | Purpose |
|---|---|
| **Python 3.10+** | Runtime language |
| **Pydantic v2** | Data validation and JSON schema generation for structured LLM output |
| **httpx** | Async/sync HTTP client for gateway communication and web fetching |
| **FastAPI + Uvicorn** | LLM Gateway V7 server |
| **MCP SDK (`mcp`)** | Model Context Protocol — stdio transport for tool dispatch |
| **asyncio** | Async orchestration of MCP sessions and tool calls |
| **FAISS (`faiss-cpu`)** | Facebook AI Similarity Search — in-process vector index for semantic memory retrieval |
| **NumPy** | Vector normalization and array operations for FAISS |

### MCP Server Dependencies

| Library | Purpose |
|---|---|
| **crawl4ai** | Headless Chromium web crawler for JS-rendered pages |
| **html2text** | HTML-to-markdown conversion (lightweight fallback for `fetch_url`) |
| **ddgs** | DuckDuckGo search (fallback when Tavily is unavailable) |
| **tavily-python** | Tavily web search API (primary search provider) |
| **python-dotenv** | Load `.env` file for API keys |

### Gateway Dependencies

| Library | Purpose |
|---|---|
| **jsonschema** | Server-side validation of structured LLM output |

---

## LLM Gateway V7

### Purpose

The gateway is a **local HTTP service** (`http://localhost:8107`) that abstracts away the differences between 7+ LLM providers behind a single unified API. Every LLM call in the agent — Perception, Decision, Memory classification — routes through it. V7 adds a `POST /v1/embed` endpoint (Ollama default, Gemini fallback, both pinned to 768-dim) used by the FAISS vector search pipeline.

### How It Works

The gateway has two pools:

**Router Pool** (4 small/fast LLMs) — classifies each request into a size tier:

| Tier | Token Range | Worker Failover Order |
|---|---|---|
| **TINY** | < 1,000 tokens | github → openrouter → groq → nvidia → cerebras → gemini → ollama |
| **LARGE** | 1,000 – 8,000 | gemini → groq → nvidia → cerebras → github → openrouter → ollama |
| **HUGE** | > 8,000 | Returns 503 (input too large) |

The router receives only `{token_count, 800-char sample}` — it never sees the system prompt, tools, schema, or context history.

**Worker Pool** (7 LLM providers) — executes the actual LLM call:

| Provider | Default Model | Notes |
|---|---|---|
| Gemini | `gemini-2.5-flash` | Available for routing |
| Groq | `llama-3.3-70b-versatile` | Fast inference |
| NVIDIA NIM | `deepseek-ai/deepseek-v3.2` | NIM-hosted |
| Cerebras | `zai-glm-4.7` | Ultra-fast inference |
| OpenRouter | configurable | Aggregator for many models |
| GitHub Models | `openai/gpt-4.1-mini` | Free tier |
| Ollama | `qwen3:4b` | Local, unlimited |

**Embedding Endpoint** (`POST /v1/embed`):

| Provider | Model | Dimension | Role |
|---|---|---|---|
| Ollama | default embedding model | 768 | Primary embedder |
| Gemini | embedding model | 768 | Fallback if Ollama unavailable |

### Routing in Agent6

| Component | Routing | Why |
|---|---|---|
| **Perception** | `provider="g"` (Gemini, router skipped) | Empirically, small-tier models couldn't reliably follow Perception's multi-step procedure |
| **Memory** (classify/record) | `provider="g"` (Gemini, router skipped) | Needs reliable structured output |
| **Decision** | `auto_route="decision"` (router picks worker) | Prompt size varies; router selects appropriate tier |
| **Embeddings** | Gateway `/v1/embed` | Ollama primary, Gemini fallback |

### Failover & Resilience

- If a provider fails, the gateway removes it from candidates and tries the next one in the tier order.
- Rate-limited providers get a cooldown backoff.
- If all providers fail, the gateway returns 503 with details of all attempts.
- Providers without API keys are silently skipped at startup.
- The `LLM_ORDER` env var controls the default failover order for non-routed calls.

---

## Setup & Run Instructions

### 1. Clone and Create Virtual Environment

```bash
cd Project/
python -m venv .venv

# Activate:
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs all core dependencies including `faiss-cpu` and `numpy` for vector search.

### 3. Configure `.env`

Create a `.env` file in the project root (see `.env.example`):

```bash
# ============================================
# REQUIRED: Gemini is needed for Perception & Memory
# Get a key at https://aistudio.google.com/apikey
# ============================================
GEMINI_API_KEY=your-gemini-api-key

# ============================================
# RECOMMENDED: Tavily for better web search
# Get a key at https://tavily.com (free tier available)
# Falls back to DuckDuckGo if missing.
# ============================================
TAVILY_API_KEY=your-tavily-api-key

# ============================================
# DECISION WORKERS: Add keys for providers you want
# the gateway to use for Decision routing.
# Providers without keys are silently skipped.
# Get free keys at each provider's site.
# ============================================
GROQ_API_KEY=your-groq-api-key
NVIDIA_API_KEY=your-nvidia-api-key
CEREBRAS_API_KEY=your-cerebras-api-key
OPEN_ROUTER_API_KEY=your-openrouter-api-key

# ============================================
# OPTIONAL: Local Ollama (no API key needed)
# Used as primary embedding provider.
# Install from https://ollama.com, then: ollama pull qwen3:4b
# ============================================
OLLAMA_MODEL=qwen3:4b
OLLAMA_URL=http://localhost:11434

# ============================================
# OPTIONAL: Gateway & routing config
# ============================================
LLM_ORDER=gemini,groq,cerebras,openrouter,ollama,nvidia
ROUTER_ORDER=cerebras,gemini,groq,nvidia
```

**Minimum requirement**: `GEMINI_API_KEY` must be set. Without it, Perception and Memory will fail. For Decision routing, add at least one additional provider key (Groq, NVIDIA, Cerebras, or OpenRouter). For embeddings, having Ollama running locally is recommended; Gemini is the fallback.

### 4. Start the LLM Gateway

```bash
cd llm_gatewayV7
python main.py
```

The gateway starts on `http://localhost:8107`. Keep this terminal running.

#### Verify the Gateway is Working

```bash
python -c "from llm_gatewayV7.client import LLM; print(LLM().chat('Say hi', max_tokens=10))"
```

To test embeddings:

```bash
python -c "from llm_gatewayV7.client import LLM; print(LLM().embed('test query'))"
```

### 5. Run the Agent

In a new terminal (with the virtual environment activated):

```bash
cd Project/
python agent6.py
```

Enter a query when prompted, or pass it as an argument:

```bash
# Interactive mode:
python agent6.py

# Direct query:
python agent6.py "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory."
```

### Sample Queries

**Query 1 — Web fetch + artifact attachment:**
```
Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date,
death date, and three key contributions to information theory.
```

**Query 2 — Multi-goal with search + weather:**
```
Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's
weather forecast there and tell me which one is most appropriate.
```

**Query 3 — Document indexing + knowledge search (RAG):**
```
Index the file papers/research.md and then answer: what are the main findings?
```

### Clearing State Between Runs

Memory and FAISS index persist across runs in `state/`. To start fresh:

```bash
# Clear memory + vector index
rm state/memory.json state/index.faiss state/index_ids.json

# Clear artifacts
rm -rf state/artifacts/*
```
