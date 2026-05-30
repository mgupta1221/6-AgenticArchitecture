"""Decision: one LLM call per turn.

Given the current goal, the relevant memory hits (descriptors only), the
recent history, and optionally the raw bytes of an artifact Perception
attached to this goal, the model picks ONE of:

  (a) answer in plain text — the answer may itself be summarisation,
      extraction, comparison, translation, or any other semantic work the
      LLM does on the attached content;
  (b) call exactly one MCP tool from the available tool list.

There is no taxonomy of "operation kinds". The model decides what it is
doing. Decision just routes the dispatch.
"""
from __future__ import annotations

import json

from models import Goal, MemoryItem, DecisionOutput, ToolCallModel

DECISION_SYSTEM = (
    "You are the Decision layer of an agent.\n"
    "Inputs you receive: ONE current goal, the relevant memory snippets,\n"
    "recent history, and optionally the raw bytes of one attached artifact.\n\n"
    "Choose EXACTLY ONE response:\n"
    "  (a) Reply with the final answer to this goal as plain text. If the\n"
    "      goal asks you to summarise, extract, compare, or transform the\n"
    "      attached content, do that work inside your reply.\n"
    "  (b) Call exactly ONE tool from the available MCP tools when you need\n"
    "      external work (fetching, file ops, time, currency, web search).\n\n"
    "Rules:\n"
    "- Never narrate. Answer or call a tool, never both.\n"
    "- Never invent a tool that is not in the tool list.\n"
    "- If the goal is already satisfied by the memory hits + history, answer\n"
    "  directly without calling a tool.\n"
    "- Artifact handles (strings starting with `art:`) are NOT file paths,\n"
    "  URLs, or tool arguments. NEVER pass an `art:...` value to read_file,\n"
    "  list_dir, fetch_url, or ANY other tool. If a goal needs the bytes of\n"
    "  an artifact, those bytes will already appear in the ATTACHED\n"
    "  ARTIFACTS section of your input — answer directly from that text.\n"
    "  WRONG:  read_file({\"path\": \"art:abc1234\"})\n"
    "  WRONG:  fetch_url({\"url\": \"art:abc1234\"})\n"
    "  RIGHT:  read the bytes already in ATTACHED ARTIFACTS and answer.\n"
    "- read_file and list_dir operate on the local sandbox/ directory, not\n"
    "  artifacts. Only call them when the user has asked you to read/list a\n"
    "  real sandbox file by name.\n"
    "- Answer using whatever is in front of you: memory hits, history, and\n"
    "  any attached artifact bytes. Be substantive — at least 3 sentences\n"
    "  or a list of items when the goal is to extract/list/select/compare.\n"
    "- For 'remember X', 'save X', 'set a reminder', 'note X' style goals,\n"
    "  call create_file (or update_file when re-saving) under the sandbox\n"
    "  with a filename describing the topic. Do NOT reply that you cannot\n"
    "  set reminders — create_file IS how you set them.\n"
    "- When the goal asks to make a file's or fetched content's contents\n"
    "  SEARCHABLE for later turns or runs (phrasings like 'index', 'ingest',\n"
    "  'make searchable', 'add to the knowledge base', 'load into memory'),\n"
    "  call `index_document`. `read_file` only returns the bytes once and\n"
    "  then discards them; `index_document` chunks the content and writes\n"
    "  the chunks into Memory so they survive across turns and runs. Use\n"
    "  `read_file` only for one-shot inspection of a known sandbox file.\n"
    "- When the goal asks to ANSWER a question and the MEMORY HITS already\n"
    "  contain `fact` items whose descriptors begin with `[sandbox:` or\n"
    "  `[art:` (those are previously-indexed chunks of source documents),\n"
    "  call `search_knowledge` against the question rather than re-fetching\n"
    "  the URL or re-reading the file. The indexed chunks are why the\n"
    "  corpus was indexed in the first place; re-fetching is wasted work.\n"
    "  The chunk text for each indexed hit is shown inline under the hit's\n"
    "  descriptor (`chunk: ...`); synthesise directly from those previews\n"
    "  rather than re-issuing the same vector query."
)


class Decision:
    def __init__(self, llm):
        self._llm = llm

    def next_step(
        self,
        goal: Goal,
        hits: list[MemoryItem],
        attached: list[tuple[str, bytes]],
        history: list[dict],
        tools: list[dict],
    ) -> DecisionOutput:
        parts = [f"GOAL: {goal.text}"]

        if hits:
            hit_summaries = [
                f"- [{h.kind}] {h.descriptor}" + (f" (artifact: {h.artifact_id})" if h.artifact_id else "")
                for h in hits
            ]
            parts.append(f"MEMORY HITS:\n" + "\n".join(hit_summaries))

        if history:
            recent = history[-10:]
            parts.append(f"HISTORY:\n{json.dumps(recent, indent=2, default=str)}")

        if attached:
            sections = []
            for aid, data in attached:
                text = data.decode("utf-8", errors="replace")[:6000]
                sections.append(f"--- {aid} ({len(data)} bytes, showing first 6000 chars) ---\n{text}")
            parts.append(f"ATTACHED ARTIFACTS:\n" + "\n".join(sections))

        prompt = "\n\n".join(parts)

        result = self._llm.chat(
            prompt=prompt,
            system=DECISION_SYSTEM,
            provider="az",
            tools=tools,
            temperature=0.3,
            max_tokens=4096,
        )

        if result.get("tool_calls"):
            tc = result["tool_calls"][0]
            args = tc.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"input": args}
            return DecisionOutput(
                tool_call=ToolCallModel(name=tc["name"], arguments=args)
            )

        return DecisionOutput(answer=result.get("text", ""))
