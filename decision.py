"""Decision component — picks one tool call or returns a final answer."""
from __future__ import annotations

import json

from models import Goal, MemoryItem, DecisionOutput, ToolCallModel

DECISION_SYSTEM = """\
You are the Decision component of an agentic system.

You are given ONE goal to work on, along with relevant history and available tools.

You MUST do exactly ONE of:
1. Call exactly ONE tool if external work is needed to resolve the goal.
2. Return a FINAL ANSWER as plain text if the goal can be resolved from the
   available context, history, or attached artifact content.

Rules:
- If an ATTACHED ARTIFACTS section is present below, use that content to answer.
- Never call more than one tool.
- Be concise and factual in final answers.
- If you have enough information to answer, answer directly — do not call a tool."""


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
            recent = history[-6:]
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
            auto_route="decision",
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
