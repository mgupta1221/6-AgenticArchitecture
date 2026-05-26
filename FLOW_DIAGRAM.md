# Agent6 — Architecture Deep Dive

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
 │  ensure_gateway()          ◄── verify LLM Gateway at localhost:8101    │
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
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  MCP SESSION OPEN                                                      │
 │  Connect to mcp_server.py via stdio                                    │
 │  Load 9 tools: web_search, fetch_url, get_time, currency_convert,     │
 │                read_file, list_dir, create_file, update_file, edit_file│
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
 │  STEP 1: memory.read(query, history)                    [NO LLM CALL]  │
 │                                                                        │
 │  Pure Python keyword-overlap search:                                   │
 │                                                                        │
 │  1. Tokenize query: {"tokyo","family","weather","saturday",...}         │
 │  2. Tokenize last 5 history events (adds recent context)               │
 │  3. For each item in memory.json:                                      │
 │     item_tokens = item.keywords + tokenize(item.descriptor)            │
 │     score = len(query_tokens & item_tokens)                            │
 │  4. Return top-8 items sorted by score                                 │
 │                                                                        │
 │  Reads from ──► ┌─────────────────────┐                                │
 │                 │  state/memory.json   │                                │
 │                 └─────────────────────┘                                │
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
 │  │ history   = last 6 events           ◄── trimmed for tokens   │      │
 │  │ tools     = [9 MCP tool defs]       ◄── name, desc, schema   │      │
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
 │  │   source: "tool:web_search",                                 │      │
 │  │   confidence: 0.9                                            │      │
 │  │ }                                                            │      │
 │  ├──────────────────────────────────────────────────────────────┤      │
 │  │ MemoryItem {                                                 │      │
 │  │   kind: "fact",                                              │      │
 │  │   descriptor: "Ueno Park is recommended for families",       │      │
 │  │   source: "tool:web_search",                                 │      │
 │  │   confidence: 0.9                                            │      │
 │  │ }                                                            │      │
 │  └────────────────────────────┬─────────────────────────────────┘      │
 │                               │                                        │
 │                               ▼                                        │
 │                    ┌─────────────────────┐                             │
 │                    │  state/memory.json   │  ◄── PERSISTED TO DISK     │
 │                    │  (append + save)     │                             │
 │                    └─────────────────────┘                             │
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

## Concrete 3-Iteration Example

```
 ══════════════════════════════════════════════════════════════════════════
 ITER 1
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► hits = [1 item from remember()]        NO LLM
                       (keyword match: "Tokyo","weather")

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
                    (1 tool_outcome + 1 fact)

 history += [{"kind":"action", "tool":"web_search",
              "artifact_id":"art:9b74...", ...}]

 ══════════════════════════════════════════════════════════════════════════
 ITER 2
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► hits = [4 items] (original + iter1)    NO LLM

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
 record_outcome ──► 2 MemoryItems saved to memory.json        LLM (Gemini)

 history += [{"kind":"action", "tool":"web_search",
              "artifact_id":"art:fd37...", ...}]

 ══════════════════════════════════════════════════════════════════════════
 ITER 3
 ══════════════════════════════════════════════════════════════════════════

 memory.read()  ─────► hits = [8 items] (growing)             NO LLM

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

 memory.read()  ─────► hits = [8 items]                       NO LLM

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
 │  │    source:"user_query", run_id:"7827d647"},               │      │
 │  │   {kind:"tool_outcome", desc:"Search results for...",     │      │
 │  │    artifact_id:"art:9b74...", source:"tool:web_search"},  │      │
 │  │   {kind:"fact", desc:"Ueno Park is recommended..."},      │      │
 │  │   {kind:"tool_outcome", desc:"Weather forecast...",       │      │
 │  │    artifact_id:"art:fd37...", source:"tool:web_search"},  │      │
 │  │   {kind:"fact", desc:"Tokyo May weather averages 22C"},   │      │
 │  │ ]                                                         │      │
 │  └───────────────────────────────────────────────────────────┘      │
 │  Written by: memory.remember(), memory.record_outcome()             │
 │  Read by:    memory.read() (keyword search, no LLM)                 │
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
 │ memory.read()          │ NO       │ Pure Python keyword match        │
 │ Perception.observe()   │ YES      │ Gemini (provider="g")            │
 │ Decision.next_step()   │ YES      │ Auto-routed (router picks tier)  │
 │ Action.execute()       │ NO       │ Pure MCP dispatch                │
 │ memory.record_outcome()│ YES      │ Gemini (provider="g")            │
 │ Agent6 loop itself     │ NO       │ Plain Python orchestration       │
 │ ArtifactStore          │ NO       │ Pure file I/O                    │
 ├────────────────────────┼──────────┼──────────────────────────────────┤
 │ TOTAL per 3-iter run   │ 9 calls  │ 1 remember + 3 perception +     │
 │                        │          │ 3 decision + 2 record_outcome    │
 └────────────────────────┴──────────┴──────────────────────────────────┘
```

---

## Q&A: Workflow Walkthrough

### How does the agent process a new query end-to-end?

When a user passes a query like *"Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast and tell me which is most appropriate"*, the following happens:

**Phase 0 — Startup**

`agent6.py` creates all component instances (`LLM`, `ArtifactStore`, `Memory`, `Perception`, `Decision`) and generates a unique `run_id`. It verifies the LLM Gateway is running at `localhost:8101` via `ensure_gateway()`. Two in-memory structures are initialized:

- `history = []` — list of dicts, tracks every event in this run, dies when the run ends.
- `prior_goals = []` — list of `Goal` objects, set once by Perception on iteration 1, then only `done` flags and `attach_artifact_id` are updated.

**Phase 1 — `memory.remember(query)`** [LLM Call — Gemini]

The raw query is sent to Memory. An LLM call classifies it and extracts keywords + a descriptor. A `MemoryItem` is created (kind: `"scratchpad"`, source: `"user_query"`) and **appended to `state/memory.json`** on disk. This allows future runs to recall what the user asked.

**Phase 2 — MCP Session Opens**

Connects to `mcp_server.py` via stdio transport. Loads the 9 available tools: `web_search`, `fetch_url`, `get_time`, `currency_convert`, `read_file`, `list_dir`, `create_file`, `update_file`, `edit_file`.

**Phase 3 — The Iteration Loop** (max 15 iterations)

Each iteration runs these steps in order:

| Step | Component | LLM? | What Happens |
|------|-----------|------|--------------|
| A | `memory.read(query, history)` | No | Pure Python keyword-overlap search across `state/memory.json`. Tokenizes the query + last 5 history events, scores each MemoryItem by keyword intersection, returns top-8 hits. |
| B | `Perception.observe(obs)` | Yes (Gemini) | Receives an `Observe` packet containing query, memory_hits, history, and prior_goals. On **iteration 1**, creates the goal list from scratch (e.g., 3 goals). On **iteration 2+**, reviews history and updates only `done` flags and `attach_artifact_id` on existing goals. Goals are **never added, removed, or reordered** (Session 6 constraint). Result is appended to history as `{kind: "perception"}`. |
| C | Completion check | No | If all goals are `done` AND an answer exists in history → **BREAK**. If all goals are `done` but no answer exists → **synthesis fallback**: creates a temporary `Goal(id="synthesis")`, attaches last 3 artifacts, calls Decision with empty tools list (forces an answer), appends to history, then **BREAK**. |
| D | Goal selection | No | Picks the first goal in `prior_goals` where `done == False`. |
| E | Artifact attachment | No | If Perception set `attach_artifact_id` on the selected goal and the artifact exists on disk, loads the raw bytes into `attached = [(artifact_id, bytes)]`. |
| F | `Decision.next_step(goal, hits, attached, history, tools)` | Yes (auto-routed) | Receives the current goal, memory hits, attached artifact bytes (if any), last 6 history events, and the 9 tool definitions. Returns **exactly one of**: a final answer (plain text) or one tool call (name + arguments). |
| G | If **answer**: append `{kind: "answer", text: "..."}` to history. Loop continues; next iteration Perception will mark the goal done. | | |
| H | If **tool call**: `Action.execute()` dispatches the MCP tool. If output > 4096 bytes, it's stored in `ArtifactStore` on disk (`.bin` + `.json`) and only a 2000-char descriptor + artifact handle are returned. If ≤ 4096 bytes, the full text stays inline. | No | |
| I | `memory.record_outcome()` | Yes (Gemini) | LLM classifies the tool result, extracts facts/preferences, creates 1+ MemoryItems **appended to `state/memory.json`**. |
| J | Append `{kind: "action", tool, arguments, result_descriptor, artifact_id}` to history. | No | |

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
- **Never written by** Perception, Decision, the completion check, or the loop itself

### 2. Why do we need both?

They answer different questions:

| | History | Memory |
|---|---|---|
| **Question it answers** | "What happened so far **in THIS run**?" | "What do I already know **from ALL past runs**?" |
| **Used by** | Perception (which goals are done?), Decision (avoid repeating tool calls) | memory.read() returns relevant past knowledge to Perception and Decision |
| **Granularity** | Every event: perception updates, tool calls, answers | Condensed: only extracted facts, tool outcomes, user preferences |
| **Lifetime** | Current run only | Persists forever (until manually deleted) |

**Example of memory helping across runs:**

- Run 1: User asks "Find Tokyo activities" → `web_search` runs → `memory.record_outcome()` stores `{kind: "fact", descriptor: "Ueno Park is recommended for families"}`
- Run 2: User asks "Plan a Tokyo trip" → `memory.read()` finds the Ueno Park fact via keyword overlap ("Tokyo") → Decision can use it directly without searching again

### 3. In what cases are LLM calls required?

Exactly **4 places** in the codebase make LLM calls:

| Component | When | Provider | Purpose |
|-----------|------|----------|---------|
| `memory.remember(query)` | Once at run start | Gemini (`provider="g"`) | Classify query into a MemoryItem |
| `Perception.observe()` | Every iteration | Gemini (`provider="g"`) | Create goals (iter 1) or update done flags (iter 2+) |
| `Decision.next_step()` | Every iteration with an unfinished goal | Auto-routed (router picks tier) | Pick one tool call or return a final answer |
| `memory.record_outcome()` | After every tool call | Gemini (`provider="g"`) | Extract facts/outcomes from tool results |

**Total for a typical 3-iteration run:** ~9 LLM calls
(1 remember + 3 perception + 3 decision + 2 record_outcome)

Everything else — `memory.read()`, `Action.execute()`, `ArtifactStore`, the Agent6 loop itself — is **pure Python with zero LLM calls**.

### 4. Can persistent memory cause false answers from keyword overlap across runs?

**Yes, this is a real and known limitation in Session 6.**

**How it happens:**

- Run 1: Query "Find Tokyo activities" → memory stores facts about activities with keywords `["Tokyo", "activities", "family"]`
- Run 2: Query "Find Tokyo restaurants" → `memory.read()` returns the activity facts because `"Tokyo"` overlaps
- Perception sees these activity-related memory hits and may incorrectly believe the "activities" goal is already satisfied
- It marks goals as done prematurely → Decision never gets called → no final answer

**Current mitigations (partial, not a full fix):**

- `memory.read()` uses keyword overlap **scoring** (not exact match) — items with more keyword overlap rank higher, so relevant items tend to beat irrelevant ones
- **Synthesis fallback** in `agent6.py`: if Perception marks all goals done but no answer exists in history, forces a Decision call with attached artifacts and empty tools list — guarantees an answer is always produced
- MemoryItems have a `run_id` field, but `memory.read()` does not filter by it (Session 6 simplification)

**Proper fixes (not implemented in S6, would be in a production system):**

- Run-scoped filtering: prioritize current-run memory items, deprioritize old ones
- Embedding-based retrieval instead of keyword matching (semantic similarity)
- Confidence decay for older items
- Manual clearing between unrelated queries: `rm state/memory.json`
