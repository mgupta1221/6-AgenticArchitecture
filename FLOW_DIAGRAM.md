# Agent 7 — Architecture Deep Dive - With memory and Retrieval

> This file is a read-only reference document. It is **not** loaded into any LLM prompt or tool call at runtime

---

## Full Orchestration Flow

```
 USER QUERY
 "Find 3 family-friendly things to do in Tokyo this weekend.
  Check Saturday's weather forecast and tell me which is most appropriate"
       │
       ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  STARTUP                                                               │
 │  run_id = "7827d647"                                                   │
 │  history = []              ◄── in-memory, dies when run ends           │
 │  prior_goals = []          ◄── in-memory, updated each iteration       │
 │  ensure_gateway()          ◄── verify LLM Gateway at localhost:8107    │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  memory.remember(query)                                    [LLM CALL]  │
 │                                                                        │
 │  LLM (Gemini) classifies the raw query text                            │
 │  Extracts: kind, keywords, descriptor, value                           │
 │                                                                        │
 │  ┌──────────────────────────────────────────────────────────────┐      │
 │  │ MemoryItem {                                                 │      │
 │  │   id: "abc12345",                                            │      │
 │  │   kind: "scratchpad",                                        │      │
 │  │   keywords: ["Tokyo","family","weather","Saturday"],          │      │
 │  │   descriptor: "A request to find family-friendly activities   │      │
 │  │                in Tokyo based on weather",                    │      │
 │  │   embedding: null,       ◄── scratchpad items skip embedding │      │
 │  │   source: "user_query",                                      │      │
 │  │   run_id: "7827d647"                                         │      │
 │  │ }                                                            │      │
 │  └────────────────────────────┬─────────────────────────────────┘      │
 │                               │                                        │
 │                               ▼                                        │
 │                    ┌─────────────────────┐                             │
 │                    │  state/memory.json   │  ◄── PERSISTED TO DISK     │
 │                    │  (append + save)     │                             │
 │                    └─────────────────────┘                             │
 │                                                                        │
 │  NOTE: Non-scratchpad items also get a 768-dim embedding via the       │
 │  gateway's /v1/embed endpoint. The embedding vector is stored both     │
 │  in the MemoryItem AND in the FAISS index on disk.                     │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  MCP SESSION OPEN                                                      │
 │  Connect to mcp_server.py via stdio                                    │
 │  Load 11 tools: web_search, fetch_url, get_time, currency_convert,    │
 │                 read_file, list_dir, create_file, update_file,         │
 │                 edit_file, index_document, search_knowledge            │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
       ┌──────────────────────────────┘
       │
       │  ╔═══════════════════════════════════════════════════════════════╗
       │  ║              ITERATION LOOP  (max 15)                        ║
       │  ╚═══════════════════════════════════════════════════════════════╝
       │
       ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 1: memory.read(query, history)                    [EMBED CALL]   │
 │                                                                        │
 │  HYBRID RETRIEVAL — vector search first, keyword fallback:             │
 │                                                                        │
 │  1. Embed the query via gateway /v1/embed (768-dim, task=retrieval_    │
 │     query). Normalize with L2.                                         │
 │  2. FAISS cosine-similarity search (IndexFlatIP) against all stored    │
 │     embeddings. Retrieve top_k * 2 candidates.                        │
 │  3. Map FAISS result indices back to MemoryItem IDs via index_ids.json │
 │  4. Filter by optional `kinds` parameter, trim to top_k.              │
 │                                                                        │
 │  If vector search returns results → return them.                       │
 │  If embedding fails OR FAISS returns nothing → KEYWORD FALLBACK:       │
 │                                                                        │
 │     a. Tokenize query: {"tokyo","family","weather","saturday",...}      │
 │     b. Tokenize last 5 history events (adds recent context)            │
 │     c. For each item in memory.json:                                   │
 │        item_tokens = item.keywords + tokenize(item.descriptor)         │
 │        score = len(query_tokens & item_tokens)                         │
 │     d. Return top-8 items sorted by score                              │
 │                                                                        │
 │  Reads from ──► ┌─────────────────────┐  ┌──────────────────────┐     │
 │                 │  state/memory.json   │  │  state/index.faiss   │     │
 │                 └─────────────────────┘  │  state/index_ids.json │     │
 │                                           └──────────────────────┘     │
 │                                                                        │
 │  Returns: hits = [MemoryItem, MemoryItem, ...]  (up to 8)             │
 │  ════════════════════════════════════════════════                       │
 │  These are "MEMORY HITS" — past knowledge relevant to current query    │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 2: Perception.observe(obs)                          [LLM CALL]   │
 │                                                     (Gemini, provider="g")
 │                                                                        │
 │  INPUT — the Observe packet:                                           │
 │  ┌──────────────────────────────────────────────────────────────┐      │
 │  │ Observe {                                                    │      │
 │  │   query: "Find 3 family-friendly things...",                 │      │
 │  │   memory_hits: [MemoryItem, ...],  ◄── from Step 1          │      │
 │  │   history: [...],                  ◄── full history so far   │      │
 │  │   prior_goals: None | [Goal,...]   ◄── None on iter 1       │      │
 │  │ }                                                            │      │
 │  └──────────────────────────────────────────────────────────────┘      │
 │                                                                        │
 │  ITER 1 (prior_goals = None):                                          │
 │    LLM creates goal list from scratch                                  │
 │    ┌────────────────────────────────────────────────────────┐          │
 │    │ Observation { goals: [                                  │          │
 │    │   Goal{id:"a1b2c3d4", text:"Search for 3 activities",  │          │
 │    │        done:false, attach_artifact_id:null},            │          │
 │    │   Goal{id:"e5f6g7h8", text:"Get weather forecast",     │          │
 │    │        done:false, attach_artifact_id:null},            │          │
 │    │   Goal{id:"i9j0k1l2", text:"Evaluate and recommend",   │          │
 │    │        done:false, attach_artifact_id:null}             │          │
 │    │ ]}                                                      │          │
 │    └────────────────────────────────────────────────────────┘          │
 │    prior_goals = observation.goals   ◄── GOALS FIXED FROM HERE ON     │
 │                                                                        │
 │  ITER 2+ (prior_goals exists):                                         │
 │    LLM reviews history, updates ONLY done flags and attach_artifact_id │
 │    Goals are NEVER added, removed, or reordered (S6 constraint)        │
 │    ┌─────────────────────────────────────────────────────────────┐     │
 │    │ for updated in observation.goals:                            │     │
 │    │   if updated.id in prior_goals:                              │     │
 │    │     prior_goals[id].done = updated.done          ◄── update  │     │
 │    │     prior_goals[id].attach_artifact_id = updated  ◄── update │     │
 │    └─────────────────────────────────────────────────────────────┘     │
 │                                                                        │
 │  HISTORY UPDATE:                                                       │
 │  history.append({"iter":N, "kind":"perception", "goals":[...]})        │
 │                                                                        │
 │          memory.json: NOT TOUCHED                                      │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 3: COMPLETION CHECK                                              │
 │                                                                        │
 │  if all(g.done for g in prior_goals):                                  │
 │      │                                                                 │
 │      ├── answer exists in history?                                     │
 │      │     YES ──► print "[done] all goals satisfied" ──► BREAK        │
 │      │                                                                 │
 │      └── NO answer in history? (Perception marked done prematurely)    │
 │            │                                                           │
 │            ▼                                                           │
 │      ┌─────────────────────────────────────────────────────┐           │
 │      │  SYNTHESIS FALLBACK                    [LLM CALL]   │           │
 │      │                                                     │           │
 │      │  Create: Goal(id="synthesis", text=original_query)  │           │
 │      │  Attach: last 3 artifacts from history              │           │
 │      │  Call: decision.next_step(goal, hits, attached,     │           │
 │      │                           history, tools=[])        │           │
 │      │                   empty tools ─┘                    │           │
 │      │         (forces LLM to answer, can't call tools)    │           │
 │      │                                                     │           │
 │      │  history.append({"kind":"answer", "text": ...})     │           │
 │      └─────────────────────────────────────────────────────┘           │
 │            │                                                           │
 │            ▼                                                           │
 │      BREAK ──────────────────────────────────────────► FINAL ANSWER    │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │ (not all done)
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 4: SELECT NEXT UNFINISHED GOAL                                   │
 │                                                                        │
 │  goal = first g in prior_goals where g.done == False                   │
 │                                                                        │
 │  ARTIFACT ATTACHMENT CHECK:                                            │
 │  if goal.attach_artifact_id and artifact exists:                       │
 │      attached = [(artifact_id, raw_bytes)]                             │
 │      print "[attach] art:96ff... (263352 bytes)"                       │
 │  else:                                                                 │
 │      attached = []                                                     │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 5: Decision.next_step(...)                          [LLM CALL]   │
 │                                              (auto_route="decision")   │
 │                                                                        │
 │  INPUT:                                                                │
 │  ┌──────────────────────────────────────────────────────────────┐      │
 │  │ goal      = Goal(id, text, done, attach_artifact_id)         │      │
 │  │ hits      = [MemoryItem, ...]       ◄── from Step 1          │      │
 │  │ attached  = [(aid, bytes), ...]     ◄── from Step 4          │      │
 │  │ history   = last 10 events          ◄── trimmed for tokens   │      │
 │  │ tools     = [11 MCP tool defs]      ◄── name, desc, schema   │      │
 │  └──────────────────────────────────────────────────────────────┘      │
 │                                                                        │
 │  PROMPT ASSEMBLED AS:                                                  │
 │  ┌──────────────────────────────────────────────────────────────┐      │
 │  │ GOAL: Search for 3 family-friendly activities in Tokyo       │      │
 │  │                                                              │      │
 │  │ MEMORY HITS:                                                 │      │
 │  │ - [scratchpad] A request to find family-friendly...          │      │
 │  │ - [fact] Ueno Park is recommended for families...            │      │
 │  │                                                              │      │
 │  │ HISTORY:                                                     │      │
 │  │ [{"iter":1, "kind":"perception", ...}, ...]                  │      │
 │  │                                                              │      │
 │  │ ATTACHED ARTIFACTS: (only if attached is non-empty)          │      │
 │  │ --- art:96ff... (263352 bytes, showing first 6000 chars) --- │      │
 │  │ [actual content here...]                                     │      │
 │  └──────────────────────────────────────────────────────────────┘      │
 │                                                                        │
 │  LLM RETURNS ONE OF:                                                   │
 │                                                                        │
 │  ┌─────────────────────┐          ┌──────────────────────────────┐     │
 │  │  ANSWER              │          │  TOOL_CALL                   │     │
 │  │  (plain text)        │          │  (name + arguments)          │     │
 │  │                      │          │                              │     │
 │  │  "Based on weather,  │          │  web_search({                │     │
 │  │   TeamLab Borderless │          │    "query": "family          │     │
 │  │   is most suitable"  │          │     activities Tokyo"        │     │
 │  │                      │          │  })                          │     │
 │  └──────────┬───────────┘          └──────────────┬───────────────┘     │
 │             │                                     │                    │
 └─────────────┼─────────────────────────────────────┼────────────────────┘
               │                                     │
       ┌───────┘                                     └───────┐
       ▼                                                     ▼
 ┌──────────────────────────────┐    ┌────────────────────────────────────┐
 │                              │    │                                    │
 │  PATH A: ANSWER              │    │  PATH B: TOOL_CALL                 │
 │                              │    │                                    │
 │  HISTORY UPDATE:             │    │  (continues below)                 │
 │  history.append({            │    │                                    │
 │    "iter": N,                │    │                                    │
 │    "kind": "answer",         │    │                                    │
 │    "goal_id": "i9j0k1l2",   │    │                                    │
 │    "text": "Based on..."    │    │                                    │
 │  })                          │    │                                    │
 │                              │    │                                    │
 │  memory.json: NOT TOUCHED    │    │                                    │
 │  artifacts: NOT TOUCHED      │    │                                    │
 │                              │    │                                    │
 │  ──► continue (next iter,    │    │                                    │
 │       Perception will mark   │    │                                    │
 │       this goal as done)     │    │                                    │
 │                              │    │                                    │
 └──────────────────────────────┘    └───────────────────┬────────────────┘
                                                         │
                                                         ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 6: Action.execute(tool_call)                    [NO LLM CALL]    │
 │                                                                        │
 │  1. Dispatch MCP tool: session.call_tool("web_search", {args})         │
 │  2. Collect all text content from result                               │
 │  3. Encode to bytes                                                    │
 │                                                                        │
 │  SIZE CHECK:                                                           │
 │                                                                        │
 │  ┌─────────────────────────────┐   ┌────────────────────────────────┐  │
 │  │  len(bytes) <= 4096         │   │  len(bytes) > 4096             │  │
 │  │  ─────────────────          │   │  ─────────────────             │  │
 │  │  IN-MEMORY                  │   │  ARTIFACT                     │  │
 │  │                             │   │                                │  │
 │  │  return (full_text, None)   │   │  descriptor = text[:2000]     │  │
 │  │                             │   │  artifact_id = store.put(...)  │  │
 │  │  Nothing stored to disk.    │   │                                │  │
 │  │  Text flows directly in    │   │  STORED TO DISK:               │  │
 │  │  history and memory.        │   │  ┌────────────────────────┐   │  │
 │  │                             │   │  │ state/artifacts/       │   │  │
 │  │  Example: get_time result   │   │  │  9b74...067a.bin ◄raw │   │  │
 │  │  is ~200 bytes, stays       │   │  │  9b74...067a.json◄meta│   │  │
 │  │  inline.                    │   │  └────────────────────────┘   │  │
 │  │                             │   │                                │  │
 │  │                             │   │  return (descriptor, art_id)   │  │
 │  │                             │   │                                │  │
 │  │                             │   │  Example: web_search returns   │  │
 │  │                             │   │  ~10KB, gets promoted.         │  │
 │  └─────────────────────────────┘   └────────────────────────────────┘  │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 7: memory.record_outcome(...)                       [LLM CALL]   │
 │                                                     (Gemini, provider="g")
 │                                                                        │
 │  INPUT:                                                                │
 │  ┌──────────────────────────────────────────────────────────────┐      │
 │  │ tool_name:   "web_search"                                    │      │
 │  │ tool_args:   {"query": "family activities Tokyo"}            │      │
 │  │ result_text: first 2000 chars of descriptor                  │      │
 │  │ artifact_id: "art:9b74..." or None                           │      │
 │  │ run_id:      "7827d647"                                      │      │
 │  │ goal_id:     "a1b2c3d4"                                     │      │
 │  └──────────────────────────────────────────────────────────────┘      │
 │                                                                        │
 │  LLM extracts 1+ MemoryItems:                                         │
 │  ┌──────────────────────────────────────────────────────────────┐      │
 │  │ MemoryItem {                                                 │      │
 │  │   kind: "tool_outcome",                                      │      │
 │  │   descriptor: "Search results for family activities Tokyo",  │      │
 │  │   artifact_id: "art:9b74...",   ◄── links to artifact        │      │
 │  │   embedding: [0.12, -0.03, ...],◄── 768-dim from /v1/embed  │      │
 │  │   source: "tool:web_search",                                 │      │
 │  │   confidence: 0.9                                            │      │
 │  │ }                                                            │      │
 │  ├──────────────────────────────────────────────────────────────┤      │
 │  │ MemoryItem {                                                 │      │
 │  │   kind: "fact",                                              │      │
 │  │   descriptor: "Ueno Park is recommended for families",       │      │
 │  │   embedding: [0.08, 0.15, ...], ◄── 768-dim from /v1/embed  │      │
 │  │   source: "tool:web_search",                                 │      │
 │  │   confidence: 0.9                                            │      │
 │  │ }                                                            │      │
 │  └────────────────────────────┬─────────────────────────────────┘      │
 │                               │                                        │
 │                               ▼                                        │
 │              ┌─────────────────────┐  ┌──────────────────────┐         │
 │              │  state/memory.json   │  │  state/index.faiss   │         │
 │              │  (append + save)     │  │  state/index_ids.json│         │
 │              └─────────────────────┘  └──────────────────────┘         │
 │              ▲ MemoryItem persisted   ▲ Embedding vector added         │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  STEP 8: HISTORY UPDATE (after tool call)                              │
 │                                                                        │
 │  history.append({                                                      │
 │    "iter": 1,                                                          │
 │    "kind": "action",                                                   │
 │    "goal_id": "a1b2c3d4",                                             │
 │    "tool": "web_search",                                               │
 │    "arguments": {"query": "family activities Tokyo"},                  │
 │    "result_descriptor": "first 2000 chars of tool output...",          │
 │    "artifact_id": "art:9b74..." or null                                │
 │  })                                                                    │
 │                                                                        │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      │  ╔════════════════════════════════╗
                                      └─►║  BACK TO STEP 1 (next iter)   ║
                                         ╚════════════════════════════════╝


 ╔═════════════════════════════════════════════════════════════════════════╗
 ║  AFTER LOOP EXITS                                                      ║
 ╚═════════════════════════════════════════════════════════════════════════╝

 ┌─────────────────────────────────────────────────────────────────────────┐
 │                                                                        │
 │  FINAL ANSWER EXTRACTION                                               │
 │                                                                        │
 │  final_answer_from(history):                                           │
 │    Scan history in REVERSE                                             │
 │    Find last event where kind == "answer"                              │
 │    Return its "text" field                                             │
 │                                                                        │
 │  If no "answer" event found: return "(no final answer produced)"       │
 │                                                                        │
 │  print("FINAL")                                                        │
 │  print(answer)                                                         │
 │                                                                        │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Vector Search & FAISS Integration

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                    EMBEDDING PIPELINE                                   │
 │                                                                        │
 │  When a MemoryItem is created (kind != "scratchpad"):                  │
 │                                                                        │
 │  1. Call gateway POST /v1/embed with the item's descriptor             │
 │     ┌──────────────────────────────────────────────────┐               │
 │     │  LLM.embed(descriptor, task_type="retrieval_     │               │
 │     │            document")                             │               │
 │     │  → 768-dim float vector                           │               │
 │     │  Providers: Ollama (default), Gemini (fallback)   │               │
 │     └──────────────────────────────────────────────────┘               │
 │                                                                        │
 │  2. Store embedding in the MemoryItem itself                           │
 │     item.embedding = [0.12, -0.03, 0.41, ...]  (768 floats)           │
 │                                                                        │
 │  3. Append to FAISS index on disk                                      │
 │     ┌──────────────────────────────────────────────────┐               │
 │     │  _index_append(item_id, embedding)                │               │
 │     │                                                   │               │
 │     │  a. Load (or create) IndexFlatIP(768)             │               │
 │     │  b. L2-normalize the vector                       │               │
 │     │  c. index.add(vector)                             │               │
 │     │  d. Append item_id to ID list                     │               │
 │     │  e. Write both to disk:                           │               │
 │     │     state/index.faiss      ◄── FAISS binary       │               │
 │     │     state/index_ids.json   ◄── ["id1","id2",...]  │               │
 │     └──────────────────────────────────────────────────┘               │
 │                                                                        │
 └─────────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────────┐
 │                    RETRIEVAL PIPELINE (memory.read)                     │
 │                                                                        │
 │  Query: "Find family-friendly activities in Tokyo"                     │
 │                                                                        │
 │  ┌─ TRY VECTOR SEARCH ────────────────────────────────────────────┐    │
 │  │                                                                 │    │
 │  │  1. Embed query (task_type="retrieval_query")                   │    │
 │  │  2. L2-normalize                                                │    │
 │  │  3. FAISS inner-product search (= cosine similarity on          │    │
 │  │     normalized vectors)                                         │    │
 │  │  4. Retrieve top_k * 2 nearest neighbor IDs                     │    │
 │  │  5. Map IDs → MemoryItems via in-memory dict                    │    │
 │  │  6. Filter by kinds if specified, trim to top_k                 │    │
 │  │                                                                 │    │
 │  │  print "[memory.read] N hits (vector)"                          │    │
 │  │  ──► return results                                             │    │
 │  └─────────────────────────────────────────────────────────────────┘    │
 │         │ embedding fails or no FAISS results                          │
 │         ▼                                                              │
 │  ┌─ KEYWORD FALLBACK ─────────────────────────────────────────────┐    │
 │  │                                                                 │    │
 │  │  1. Tokenize query + last 5 history events                      │    │
 │  │  2. Score each MemoryItem by keyword overlap                    │    │
 │  │  3. Sort by overlap, return top_k                               │    │
 │  │                                                                 │    │
 │  │  print "[memory.read] N hits (keyword fallback)"                │    │
 │  │  ──► return results                                             │    │
 │  └─────────────────────────────────────────────────────────────────┘    │
 │                                                                        │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Document Indexing & Knowledge Search

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  index_document(path) — MCP Tool                                       │
 │                                                                        │
 │  Purpose: Chunk a file or artifact and write each chunk into Memory    │
 │  as a searchable `fact` with an embedding. Enables later vector        │
 │  queries via search_knowledge.                                         │
 │                                                                        │
 │  1. Read content from sandbox file or artifact (art:xxx)               │
 │                                                                        │
 │  2. Sliding-window chunking                                            │
 │     ┌────────────────────────────────────────────────────────────┐     │
 │     │  chunk_size = 400 words (default)                          │     │
 │     │  overlap    = 80 words (default)                           │     │
 │     │  stride     = chunk_size - overlap = 320 words             │     │
 │     │                                                            │     │
 │     │  ┌─────────────────────────────────────┐                  │     │
 │     │  │ chunk 1: words[0..399]              │                  │     │
 │     │  │         ┌──────────────────────────────────┐           │     │
 │     │  │         │ chunk 2: words[320..719]         │           │     │
 │     │  │         │         ┌─────────────────────────────┐     │     │
 │     │  │         │         │ chunk 3: words[640..1039]   │     │     │
 │     │  └─────────┼─────────┼────────────────────────────┘     │     │
 │     │            └─────────┼────────────────────────────────────┘     │
 │     │                      └── 80-word overlap between chunks         │
 │     └────────────────────────────────────────────────────────────┘     │
 │                                                                        │
 │  3. For each chunk → memory.add_fact()                                 │
 │     - descriptor: "[sandbox:file.md chunk 1/5] preview..."             │
 │     - value.chunk: full chunk text                                     │
 │     - keywords: top-20 words from chunk                                │
 │     - embedding: 768-dim vector via /v1/embed                          │
 │     - Appended to memory.json + FAISS index                            │
 │                                                                        │
 └─────────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────────┐
 │  search_knowledge(query, k) — MCP Tool                                 │
 │                                                                        │
 │  Purpose: Vector search over indexed `fact` chunks.                    │
 │  Calls memory.read(query, kinds=["fact"], top_k=k)                     │
 │                                                                        │
 │  Returns ranked chunks with provenance:                                │
 │  [                                                                     │
 │    {                                                                   │
 │      "id": "abc12345",                                                 │
 │      "descriptor": "[sandbox:spec.md chunk 2/5] ...",                  │
 │      "source": "sandbox:spec.md",                                      │
 │      "chunk_preview": "first 240 chars of chunk...",                   │
 │      "metadata": { chunk_index, total_chunks, source }                 │
 │    },                                                                  │
 │    ...                                                                 │
 │  ]                                                                     │
 │                                                                        │
 │  Decision sees these results and synthesizes answers from them          │
 │  without re-fetching the original source.                              │
 │                                                                        │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## Concrete 3-Iteration Example

```
 ══════════════════════════════════════════════════════════════════════════
 ITER 1
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► Embed query → FAISS search             EMBED CALL
                       hits = [1 item from remember()]
                       (cosine similarity on "Tokyo","weather")
                       Falls back to keyword if no FAISS index yet

 Perception     ─────► Creates 3 goals:                       LLM (Gemini)
                       g:a1b2 "Search activities"    done=F
                       g:e5f6 "Get weather"          done=F
                       g:i9j0 "Evaluate+recommend"   done=F
                       prior_goals SET (fixed from now on)

 history += [{"kind":"perception", "goals":[...]}]

 All done? NO

 Select goal ──► g:a1b2 (first unfinished)
 Attached?   ──► No

 Decision    ─────► TOOL_CALL: web_search("activities")       LLM (auto-route)
 Action      ─────► 10KB result → ARTIFACT created            NO LLM
                    art:9b74... stored in state/artifacts/
 record_outcome ──► 2 MemoryItems saved to memory.json        LLM (Gemini)
                    + embedded (768-dim) + indexed in FAISS
                    (1 tool_outcome + 1 fact)

 history += [{"kind":"action", "tool":"web_search",
              "artifact_id":"art:9b74...", ...}]

 ══════════════════════════════════════════════════════════════════════════
 ITER 2
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► FAISS cosine search → 4 hits (vector)  EMBED CALL

 Perception     ─────► Reviews history, marks g:a1b2 DONE     LLM (Gemini)
                       g:a1b2 "Search activities"    done=T ✓
                       g:e5f6 "Get weather"          done=F
                       g:i9j0 "Evaluate+recommend"   done=F

 history += [{"kind":"perception", "goals":[...]}]

 All done? NO

 Select goal ──► g:e5f6 (next unfinished)
 Attached?   ──► No

 Decision    ─────► TOOL_CALL: web_search("Tokyo weather")    LLM (auto-route)
 Action      ─────► 10KB result → ARTIFACT created            NO LLM
                    art:fd37... stored in state/artifacts/
 record_outcome ──► 2 MemoryItems + embeddings + FAISS index  LLM (Gemini)

 history += [{"kind":"action", "tool":"web_search",
              "artifact_id":"art:fd37...", ...}]

 ══════════════════════════════════════════════════════════════════════════
 ITER 3
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► FAISS cosine search → 8 hits (vector)  EMBED CALL

 Perception     ─────► Marks g:e5f6 DONE                      LLM (Gemini)
                       Sets attach_artifact_id on g:i9j0
                       g:a1b2 "Search activities"    done=T ✓
                       g:e5f6 "Get weather"          done=T ✓
                       g:i9j0 "Evaluate+recommend"   done=F
                              attach=art:9b74...

 history += [{"kind":"perception", "goals":[...]}]

 All done? NO (g:i9j0 still open)

 Select goal ──► g:i9j0 (last unfinished)
 Attached?   ──► YES! Load art:9b74... bytes from disk
                 attached = [("art:9b74...", <10284 bytes>)]
                 print "[attach] art:9b74... (10284 bytes)"

 Decision    ─────► Sees ATTACHED ARTIFACTS section            LLM (auto-route)
                    Has enough data to answer directly
                    ANSWER: "Based on the weather showing
                    sunny 22C, TeamLab Borderless is most
                    appropriate because..."

 history += [{"kind":"answer", "goal_id":"i9j0",
              "text":"Based on the weather..."}]

 ══════════════════════════════════════════════════════════════════════════
 ITER 4
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► FAISS cosine search → 8 hits (vector)  EMBED CALL

 Perception     ─────► Sees answer in history for g:i9j0       LLM (Gemini)
                       Marks g:i9j0 DONE
                       g:a1b2 "Search activities"    done=T ✓
                       g:e5f6 "Get weather"          done=T ✓
                       g:i9j0 "Evaluate+recommend"   done=T ✓

 All done? YES
 Answer in history? YES ("Based on the weather...")

 print "[done] all goals satisfied"
 BREAK

 ══════════════════════════════════════════════════════════════════════════
 FINAL
 ══════════════════════════════════════════════════════════════════════════

 final_answer_from(history) scans backwards, finds:
   {"kind": "answer", "text": "Based on the weather..."}

 PRINTS:
 "Based on the weather showing sunny 22C, TeamLab Borderless
  is most appropriate because it offers an immersive indoor
  experience that works in any weather..."
```

---

## State Summary: What Lives Where

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                        IN-MEMORY (current run only)                 │
 │                                                                     │
 │  history = [                          prior_goals = [               │
 │    {kind:"perception", goals:[...]},    Goal(a1b2, done=T),         │
 │    {kind:"action", tool:"web_.."},      Goal(e5f6, done=T),         │
 │    {kind:"perception", goals:[...]},    Goal(i9j0, done=T),         │
 │    {kind:"action", tool:"web_.."},    ]                             │
 │    {kind:"perception", goals:[...]},                                │
 │    {kind:"answer", text:"Based.."},   hits = [MemoryItem x 8]      │
 │    {kind:"perception", goals:[...]},                                │
 │  ]                                                                  │
 │                                                                     │
 │  DIES WHEN RUN ENDS. Not saved anywhere.                            │
 └─────────────────────────────────────────────────────────────────────┘

 ┌─────────────────────────────────────────────────────────────────────┐
 │                        ON DISK (persists across runs)               │
 │                                                                     │
 │  state/memory.json                                                  │
 │  ┌───────────────────────────────────────────────────────────┐      │
 │  │ [                                                         │      │
 │  │   {kind:"scratchpad", desc:"A request to find...",        │      │
 │  │    embedding: null,                                       │      │
 │  │    source:"user_query", run_id:"7827d647"},               │      │
 │  │   {kind:"tool_outcome", desc:"Search results for...",     │      │
 │  │    artifact_id:"art:9b74...",                             │      │
 │  │    embedding: [0.12, -0.03, ...],  ◄── 768-dim vector     │      │
 │  │    source:"tool:web_search"},                             │      │
 │  │   {kind:"fact", desc:"Ueno Park is recommended...",       │      │
 │  │    embedding: [0.08, 0.15, ...],   ◄── 768-dim vector     │      │
 │  │    source:"tool:web_search"},                             │      │
 │  │ ]                                                         │      │
 │  └───────────────────────────────────────────────────────────┘      │
 │  Written by: memory.remember(), memory.record_outcome(),            │
 │              memory.add_fact()                                       │
 │  Read by:    memory.read() (vector search + keyword fallback)       │
 │                                                                     │
 │  state/index.faiss          ◄── FAISS IndexFlatIP binary            │
 │  ┌───────────────────────────────────────────────────────────┐      │
 │  │ Binary file, IndexFlatIP(768)                             │      │
 │  │ Contains L2-normalized 768-dim vectors for all non-       │      │
 │  │ scratchpad MemoryItems. Supports inner-product search     │      │
 │  │ (equivalent to cosine similarity on normalized vectors).  │      │
 │  └───────────────────────────────────────────────────────────┘      │
 │  Written by: _index_append() (called from _persist_item)            │
 │  Read by:    _vector_search() (called from memory.read)             │
 │                                                                     │
 │  state/index_ids.json       ◄── maps FAISS row → MemoryItem ID     │
 │  ┌───────────────────────────────────────────────────────────┐      │
 │  │ ["abc12345", "def67890", "ghi13579", ...]                 │      │
 │  │ Index i in this array corresponds to row i in the FAISS   │      │
 │  │ index. Used to map search results back to MemoryItems.    │      │
 │  └───────────────────────────────────────────────────────────┘      │
 │                                                                     │
 │  state/artifacts/                                                   │
 │  ┌───────────────────────────────────────────────────────────┐      │
 │  │ 9b74837b067a4cd7.bin  ← raw search results (10KB)        │      │
 │  │ 9b74837b067a4cd7.json ← {id, content_type, size, source} │      │
 │  │ fd37328f35c8ee75.bin  ← raw weather results (10KB)        │      │
 │  │ fd37328f35c8ee75.json ← metadata                          │      │
 │  └───────────────────────────────────────────────────────────┘      │
 │  Written by: Action (when output > 4096 bytes)                      │
 │  Read by:    agent6 loop (when Perception attaches to a goal)       │
 │  Content-addressable: identical content deduplicates                 │
 │                                                                     │
 │  sandbox/                                                           │
 │  ┌───────────────────────────────────────────────────────────┐      │
 │  │ Files created/read/edited by MCP file tools ONLY          │      │
 │  │ Security boundary: _safe() prevents path traversal        │      │
 │  │ LLM cannot access .env, source code, or anything outside  │      │
 │  └───────────────────────────────────────────────────────────┘      │
 │                                                                     │
 └─────────────────────────────────────────────────────────────────────┘
```

---

## LLM Call Summary Per Iteration

```
 ┌────────────────────────┬──────────┬──────────────────────────────────┐
 │ Component              │ LLM?     │ Provider                         │
 ├────────────────────────┼──────────┼──────────────────────────────────┤
 │ memory.remember()      │ YES (1x) │ Gemini (provider="g")            │
 │ memory.read()          │ EMBED    │ Gateway /v1/embed (query vector) │
 │                        │          │ + FAISS search (no LLM)          │
 │                        │          │ Fallback: Pure Python keywords   │
 │ Perception.observe()   │ YES      │ Gemini (provider="g")            │
 │ Decision.next_step()   │ YES      │ Auto-routed (router picks tier)  │
 │ Action.execute()       │ NO       │ Pure MCP dispatch                │
 │ memory.record_outcome()│ YES      │ Gemini (provider="g")            │
 │ memory.add_fact()      │ EMBED    │ Gateway /v1/embed (doc vector)   │
 │ Agent6 loop itself     │ NO       │ Plain Python orchestration       │
 │ ArtifactStore          │ NO       │ Pure file I/O                    │
 ├────────────────────────┼──────────┼──────────────────────────────────┤
 │ TOTAL per 3-iter run   │ ~9 LLM   │ 1 remember + 3 perception +     │
 │                        │ + embeds │ 3 decision + 2 record_outcome   │
 │                        │          │ + ~6 embed calls (query + store) │
 └────────────────────────┴──────────┴──────────────────────────────────┘
```

---

## Q&A: Workflow Walkthrough

### How does the agent process a new query end-to-end?

When a user passes a query like *"Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast and tell me which is most appropriate"*, the following happens:

**Phase 0 — Startup**

`agent6.py` creates all component instances (`LLM`, `ArtifactStore`, `Memory`, `Perception`, `Decision`) and generates a unique `run_id`. It verifies the LLM Gateway V7 is running at `localhost:8107` via `ensure_gateway()`. Two in-memory structures are initialized:

- `history = []` — list of dicts, tracks every event in this run, dies when the run ends.
- `prior_goals = []` — list of `Goal` objects, set once by Perception on iteration 1, then only `done` flags and `attach_artifact_id` are updated.

**Phase 1 — `memory.remember(query)*`* [LLM Call — Gemini]

The raw query is sent to Memory. An LLM call (Gemini, `provider="g"`) classifies it and extracts keywords + a descriptor. A `MemoryItem` is created (kind: `"scratchpad"`, source: `"user_query"`). Since scratchpad items skip embedding, no vector is generated. The item is **appended to `state/memory.json`** on disk.

**Phase 2 — MCP Session Opens**

Connects to `mcp_server.py` via stdio transport. Loads the 11 available tools: `web_search`, `fetch_url`, `get_time`, `currency_convert`, `read_file`, `list_dir`, `create_file`, `update_file`, `edit_file`, `index_document`, `search_knowledge`.

**Phase 3 — The Iteration Loop** (max 15 iterations)

Each iteration runs these steps in order:


| Step | Component                                                                                                                                                                                                                                                    | LLM?                 | What Happens                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A    | `memory.read(query, history)`                                                                                                                                                                                                                                | Embed only           | **Hybrid retrieval**: first embeds the query via gateway `/v1/embed` (768-dim, `task_type="retrieval_query"`), then runs FAISS cosine-similarity search across all stored embeddings. If vector search returns results, uses those. If embedding fails or FAISS has no entries, falls back to pure Python keyword-overlap search. Returns top-8 hits.                                                              |
| B    | `Perception.observe(obs)`                                                                                                                                                                                                                                    | Yes (Gemini)         | Receives an `Observe` packet containing query, memory_hits, history, and prior_goals. On **iteration 1**, creates the goal list from scratch. On **iteration 2+**, reviews history and updates only `done` flags and `attach_artifact_id` on existing goals. Goals are **never added, removed, or reordered** (Session 6 constraint). Result is appended to history as `{kind: "perception"}`.                     |
| C    | Completion check                                                                                                                                                                                                                                             | No                   | If all goals are `done` AND an answer exists in history → **BREAK**. If all goals are `done` but no answer exists → **synthesis fallback**: creates a temporary `Goal(id="synthesis")`, attaches last 3 artifacts, calls Decision with empty tools list (forces an answer), appends to history, then **BREAK**.                                                                                                    |
| D    | Goal selection                                                                                                                                                                                                                                               | No                   | Picks the first goal in `prior_goals` where `done == False`.                                                                                                                                                                                                                                                                                                                                                       |
| E    | Artifact attachment                                                                                                                                                                                                                                          | No                   | If Perception set `attach_artifact_id` on the selected goal and the artifact exists on disk, loads the raw bytes into `attached = [(artifact_id, bytes)]`.                                                                                                                                                                                                                                                         |
| F    | `Decision.next_step(goal, hits, attached, history, tools)`                                                                                                                                                                                                   | Yes (auto-routed)    | Receives the current goal, memory hits, attached artifact bytes (if any), last 10 history events, and the 11 tool definitions. Returns **exactly one of**: a final answer (plain text) or one tool call (name + arguments). Decision is aware of `index_document` and `search_knowledge` tools — it uses `index_document` for "make searchable" goals and `search_knowledge` for "query the knowledge base" goals. |
| G    | If **answer**: append `{kind: "answer", text: "..."}` to history. Loop continues; next iteration Perception will mark the goal done.                                                                                                                         |                      |                                                                                                                                                                                                                                                                                                                                                                                                                    |
| H    | If **tool call**: `Action.execute()` dispatches the MCP tool. If output > 4096 bytes, it's stored in `ArtifactStore` on disk (`.bin` + `.json`) and only a 2000-char descriptor + artifact handle are returned. If ≤ 4096 bytes, the full text stays inline. | No                   |                                                                                                                                                                                                                                                                                                                                                                                                                    |
| I    | `memory.record_outcome()`                                                                                                                                                                                                                                    | Yes (Gemini) + Embed | LLM classifies the tool result, extracts facts/preferences, creates 1+ MemoryItems. Each non-scratchpad item gets a 768-dim embedding via `/v1/embed` and is **appended to both `state/memory.json` AND the FAISS index** on disk.                                                                                                                                                                                 |
| J    | Append `{kind: "action", tool, arguments, result_descriptor, artifact_id}` to history.                                                                                                                                                                       | No                   |                                                                                                                                                                                                                                                                                                                                                                                                                    |


**Phase 4 — Final Answer**

After the loop exits, `final_answer_from(history)` scans history in reverse and returns the text of the last `{kind: "answer"}` event. This is printed to the user.

---

## Q&A: History vs Memory, LLM Calls, and Cross-Run Behavior

### 1. When is history updated vs when is memory.json updated?

**History** (in-memory `list[dict]`, lifetime = current run only):

- After **Perception** runs → `{kind: "perception", goals: [...]}`
- After **Decision** returns an answer → `{kind: "answer", goal_id, text}`
- After **Action** completes a tool call → `{kind: "action", tool, arguments, result_descriptor, artifact_id}`
- **Never saved to disk.** Dies when the run ends.

**memory.json** (persistent on disk, survives across runs):

- Written by `memory.remember(query)` — once at run start
- Written by `memory.record_outcome()` — after every tool call (1+ MemoryItems per call)
- Written by `memory.add_fact()` — during document indexing (via `index_document` tool)
- **Never written by** Perception, Decision, the completion check, or the loop itself

**FAISS index** (persistent on disk, survives across runs):

- Written alongside memory.json whenever a non-scratchpad MemoryItem is persisted
- `state/index.faiss` — the binary FAISS index (IndexFlatIP, 768-dim)
- `state/index_ids.json` — maps FAISS row index to MemoryItem ID

### 2. Why do we need both history and memory?

They answer different questions:


|                         | History                                                                   | Memory                                                                   |
| ----------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| **Question it answers** | "What happened so far **in THIS run**?"                                   | "What do I already know **from ALL past runs**?"                         |
| **Used by**             | Perception (which goals are done?), Decision (avoid repeating tool calls) | memory.read() returns relevant past knowledge to Perception and Decision |
| **Granularity**         | Every event: perception updates, tool calls, answers                      | Condensed: only extracted facts, tool outcomes, user preferences         |
| **Lifetime**            | Current run only                                                          | Persists forever (until manually deleted)                                |
| **Retrieval**           | Sequential scan                                                           | FAISS vector search (primary) + keyword overlap (fallback)               |


**Example of memory helping across runs:**

- Run 1: User asks "Find Tokyo activities" → `web_search` runs → `memory.record_outcome()` stores `{kind: "fact", descriptor: "Ueno Park is recommended for families", embedding: [0.08, 0.15, ...]}` + adds vector to FAISS
- Run 2: User asks "Plan a Tokyo trip" → `memory.read()` embeds "Plan a Tokyo trip", FAISS finds the Ueno Park fact via cosine similarity → Decision can use it directly without searching again

### 3. In what cases are LLM calls required?

Exactly **4 places** in the codebase make LLM calls, plus embedding calls:


| Component                 | When                                                                       | Provider                              | Purpose                                              |
| ------------------------- | -------------------------------------------------------------------------- | ------------------------------------- | ---------------------------------------------------- |
| `memory.remember(query)`  | Once at run start                                                          | Gemini (`provider="g"`)               | Classify query into a MemoryItem                     |
| `Perception.observe()`    | Every iteration                                                            | Gemini (`provider="g"`)               | Create goals (iter 1) or update done flags (iter 2+) |
| `Decision.next_step()`    | Every iteration with an unfinished goal                                    | Auto-routed (`auto_route="decision"`) | Pick one tool call or return a final answer          |
| `memory.record_outcome()` | After every tool call                                                      | Gemini (`provider="g"`)               | Extract facts/outcomes from tool results             |
| `memory._try_embed()`     | After each non-scratchpad MemoryItem creation + each `memory.read()` query | Gateway `/v1/embed` (Ollama/Gemini)   | Generate 768-dim embedding vectors                   |


**Total for a typical 3-iteration run:** ~9 LLM calls + ~6 embedding calls
(1 remember + 3 perception + 3 decision + 2 record_outcome + 3 query embeds + 3 item embeds)

Everything else — `memory.read()` FAISS search, `Action.execute()`, `ArtifactStore`, the Agent6 loop itself — is **pure Python with zero LLM calls**.

### 4. How does vector search improve over keyword-only retrieval?

**Keyword overlap (base version):**

- "Plan a Tokyo trip" matches memory items containing the word "Tokyo"
- Also matches "Tokyo population census" (same keyword, wrong intent)
- Misses "Explore Japanese capital" (different words, same meaning)

**FAISS vector search (current version):**

- "Plan a Tokyo trip" is embedded to a 768-dim vector
- Cosine similarity finds semantically related items regardless of exact words
- "Explore Japanese capital" scores high (similar meaning)
- "Tokyo population census" scores lower (different intent)
- Falls back to keyword search if embedding service is unavailable

**The hybrid approach ensures retrieval always works** — vector search for quality, keyword fallback for resilience.

### 5. How does document indexing work?

The `index_document` MCP tool enables a **RAG (Retrieval-Augmented Generation)** workflow:

1. **Index phase**: User says "make this file searchable" → Decision calls `index_document` → file is chunked (400-word sliding window, 80-word overlap) → each chunk becomes a `fact` MemoryItem with an embedding → stored in memory.json + FAISS index
2. **Query phase**: User asks a question about the indexed content → Decision calls `search_knowledge` → vector search over `fact` chunks → returns top-k ranked chunks with provenance → Decision synthesizes the answer

This avoids re-fetching or re-reading source files and enables semantic search over large documents across runs.