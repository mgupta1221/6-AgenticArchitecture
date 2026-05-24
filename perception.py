"""Perception component — goal planner and tracker."""
from __future__ import annotations

import json

from models import Observe, Observation

PERCEPTION_SYSTEM = """\
You are the Perception component of an agentic system.

FIRST ITERATION (prior_goals is null):
  Break the user's query into a list of bounded, actionable goals.
  Each goal must be achievable with a single tool call or a direct answer.
  Assign each goal a unique 8-character hex id.

SUBSEQUENT ITERATIONS (prior_goals is present):
  Review the history and update done flags.
  Do NOT add, remove, or reorder goals.
  A goal is done when history shows it was resolved — either by a successful
  tool result or by a decision answer for that goal.

For attach_artifact_id:
  Set it to an artifact id (e.g. "art:96ff2446efcb6b4c") when the NEXT
  unfinished goal needs to consume that artifact's raw bytes.
  Otherwise set it to null.

Return the full goal list every time, with updated done flags."""


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
            provider="g",
            response_format={"type": "json_schema", "schema": schema, "name": "observation"},
            temperature=0.3,
            max_tokens=2048,
        )

        parsed = result.get("parsed") or json.loads(result["text"])
        return Observation(**parsed)
