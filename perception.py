"""Perception: the agent's orchestrator.

Runs every loop iteration. Looks at the user's original query, the memory
hits, and the run history so far, and emits the current Observation —
which goals exist, which are done, and whether the next unfinished goal
needs raw bytes from a specific artifact.

Perception never reads artifact bytes. It sees handles + descriptors only.
When a goal needs bytes, Perception flips `send_artifact: true` and points
`artifact_index` at one of the artifacts listed in MEMORY HITS. The outer
loop resolves the index back to the artifact id and attaches the bytes.
"""
from __future__ import annotations

import json

from models import Observe, Observation

PERCEPTION_SYSTEM =  (
    "You are the Perception layer of an agent.\n"
    "Each iteration you see the user's query, the prior goal list, the\n"
    "current memory hits (descriptors only — never raw bytes), and the run\n"
    "history. Return the CURRENT goal list as JSON matching the schema.\n\n"
    "Goals are identified by POSITION in the output array. Always return\n"
    "the goals in the SAME ORDER as PRIOR GOALS. Do not reorder, do not\n"
    "drop a prior goal, do not add a goal in the middle.\n"
    "You MAY append new goals at the END when a discovery action on a\n"
    "prior turn (for example, listing the contents of a directory) reveals\n"
    "concrete items that were unknown at decomposition time. In that case\n"
    "keep all prior goals verbatim and append one new goal per concrete\n"
    "item, then re-append the original synthesis/report goal LAST so it\n"
    "stays the final step.\n\n"
    "You speak at the level of INTENT, not tool selection. Write each goal\n"
    "as a short imperative describing WHAT must happen, not WHICH tool\n"
    "will do it. Decision is the layer that maps intent to a tool; leave\n"
    "that choice to Decision. Example intent verbs you may use: fetch,\n"
    "open, list, look up the time, convert currency, save a note, make\n"
    "this content searchable, query the existing knowledge base, extract,\n"
    "summarise, compare, synthesise. Do not name specific tools.\n\n"
    "Procedure:\n"
    "1. If PRIOR GOALS is empty, decompose the query into one or more short\n"
    "   imperative goals (one per distinct part). If the query asks to\n"
    "   read/fetch/process N items (\"top 3 results\", \"first 5 articles\"),\n"
    "   emit a SEPARATE fetch goal for each item plus the final\n"
    "   synthesis goal — NOT a single umbrella goal.\n"
    "   If the query asks to ingest N files so they can be searched\n"
    "   later, emit one goal per file expressing that its content should\n"
    "   be made searchable, plus a final report goal.\n"
    "   If MEMORY HITS already contain `fact` items whose descriptors\n"
    "   start with `[sandbox:` or `[art:` (these mark previously-indexed\n"
    "   chunks of source documents), the next goal for any question\n"
    "   about that material is to QUERY THE EXISTING KNOWLEDGE BASE\n"
    "   rather than to re-fetch or re-open the original sources. Pair\n"
    "   that query goal with a final synthesis/answer goal — never emit\n"
    "   a knowledge-base query as the only goal, because the user still\n"
    "   needs an answer produced from the returned chunks.\n"
    "   Whenever the user's query is a question (rather than a pure\n"
    "   action like 'save X' or 'fetch Y'), the LAST goal in your\n"
    "   decomposition must be a synthesis/answer goal that emits the\n"
    "   final reply (verbs like answer, tell, summarise, compare, list,\n"
    "   extract, identify, describe).\n"
    "2. Otherwise copy each prior goal's `text` verbatim into the same slot.\n"
    "   Mark `done: true` the moment RUN HISTORY shows an action satisfying\n"
    "   it. Once done, leave it done in every later iteration.\n"
    "3. For the FIRST unfinished goal (lowest-index slot with done=false),\n"
    "   set `send_artifact: true` whenever ANY of these apply:\n"
    "     - the goal text contains extract / summarise / list / synthesise /\n"
    "       analyse / evaluate / select / compare / pick / choose / decide;\n"
    "     - the goal needs information that lives inside a fetched page or\n"
    "       file rather than just in the short descriptor.\n"
    "   In that case pick `artifact_index` = the `i` value (0, 1, 2, ...)\n"
    "   of the most relevant MEMORY HITS entry (entries whose `i` is null\n"
    "   are not artifacts and cannot be picked). When in doubt, attach the\n"
    "   most recent artifact whose descriptor matches the goal topic.\n"
    "4. Only when the goal is purely fetch / search / compute / open / time\n"
    "   should you leave `send_artifact: false` and `artifact_index: null`.\n\n"
    "Example. Given\n"
    '  MEMORY HITS: [{"i":0,"artifact_id":"art:aaa","descriptor":'
    '"page fetch result -> art:aaa"}]\n'
    '  PRIOR GOALS: [{"text":"Fetch the page","done":false,'
    '"send_artifact":false,"artifact_index":null},\n'
    '                {"text":"Extract X","done":false,'
    '"send_artifact":false,"artifact_index":null}]\n'
    "return:\n"
    '  {"goals":[\n'
    '    {"text":"Fetch the page","done":true,'
    '"send_artifact":false,"artifact_index":null},\n'
    '    {"text":"Extract X","done":false,'
    '"send_artifact":true,"artifact_index":0}\n'
    "  ]}"
)

class Perception:
    def __init__(self, llm):
        self._llm = llm

    def observe(self, obs: Observe) -> Observation:
        schema = Observation.model_json_schema()

        parts = [f"QUERY: {obs.query}"]

        if obs.memory_hits:
            hits = json.dumps(
                [h.model_dump(mode="json") for h in obs.memory_hits],
                indent=2,
                default=str,
            )
            parts.append(f"MEMORY HITS:\n{hits}")

        if obs.history:
            parts.append(f"HISTORY:\n{json.dumps(obs.history, indent=2, default=str)}")

        if obs.prior_goals is not None:
            goals = json.dumps([g.model_dump() for g in obs.prior_goals], indent=2)
            parts.append(f"PRIOR GOALS:\n{goals}")
        else:
            parts.append("PRIOR GOALS: null (first iteration — create the initial goal list)")

        prompt = "\n\n".join(parts)

        result = self._llm.chat(
            prompt=prompt,
            system=PERCEPTION_SYSTEM,
            # auto_route="perception",
            provider="az",
            response_format={"type": "json_schema", "schema": schema, "name": "observation"},
            temperature=0.3,
            max_tokens=2048,
        )

        parsed = result.get("parsed") or json.loads(result["text"])
        return Observation(**parsed)
