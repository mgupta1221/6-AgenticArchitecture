"""
Agent6 loop — the outer orchestrator.

Plain Python program, no LLM calls. All intelligence lives in:
  Perception (LLM), Decision (LLM), Memory.record_outcome (LLM), Action (no LLM).
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from llm_gatewayV7.client import LLM
from models import Goal, Observe
from artifact_store import ArtifactStore
from memory import Memory
from perception import Perception
from decision import Decision
from action import Action

MAX_ITERATIONS = 15
GATEWAY_URL = "http://localhost:8107"


def _goal_prefix(g: Goal) -> str:
    return " " if g.done else "o"


def ensure_gateway():
    try:
        r = httpx.get(f"{GATEWAY_URL}/v1/providers", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"[error] Gateway not reachable at {GATEWAY_URL}: {e}")
        print("Start it first: cd llm_gatewayV7 && python main.py")
        sys.exit(1)


@asynccontextmanager
async def mcp_session():
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py")],
    )
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            try:
                yield session
            except asyncio.CancelledError:
                pass


async def load_tools(session: ClientSession):
    result = await session.list_tools()
    print(f"[mcp] loaded {len(result.tools)} tools: {[t.name for t in result.tools]}\n")
    return result.tools


def mcp_tools_for_decision(mcp_tools) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


def final_answer_from(history: list[dict]) -> str:
    for event in reversed(history):
        if event.get("kind") == "answer":
            return event["text"]
    return "(no final answer produced)"


async def run(query: str) -> str:
    ensure_gateway()
    run_id = uuid.uuid4().hex[:8]
    print(f"\nrun {run_id}")
    print(f"— query: {query}\n")

    llm = LLM()
    artifacts = ArtifactStore()
    memory = Memory(llm)
    perception = Perception(llm)
    decision = Decision(llm)

    history: list[dict] = []
    prior_goals: list[Goal] = []

    memory.remember(query, source="user_query", run_id=run_id)

    async with mcp_session() as session:
        mcp_tools = await load_tools(session)
        tools = mcp_tools_for_decision(mcp_tools)
        action = Action(session, artifacts)

        for it in range(1, MAX_ITERATIONS + 1):
            print(f"iter {it}")

            # ── memory.read ──
            hits = memory.read(query, history)
            print(f"[memory.read] {len(hits)} hits")

            # ── perception ──
            obs = Observe(
                query=query,
                memory_hits=hits,
                history=history,
                prior_goals=prior_goals or None,
            )
            observation = perception.observe(obs)

            if not prior_goals:
                prior_goals = observation.goals
            else:
                goal_map = {g.id: g for g in prior_goals}
                for updated in observation.goals:
                    if updated.id in goal_map:
                        goal_map[updated.id].done = updated.done
                        goal_map[updated.id].attach_artifact_id = updated.attach_artifact_id

            for g in prior_goals:
                attach = f" attach — {g.attach_artifact_id}" if g.attach_artifact_id else ""
                print(f"[perception] {_goal_prefix(g)} g:{g.id} — {g.text}{attach}")

            history.append({
                "iter": it,
                "kind": "perception",
                "goals": [g.model_dump() for g in prior_goals],
            })

            # ── check completion ──
            if all(g.done for g in prior_goals):
                if final_answer_from(history) != "(no final answer produced)":
                    print("[done] all goals satisfied")
                    break
                # Perception says done but Decision never answered — force a synthesis
                print("[done] goals satisfied but no answer yet — forcing synthesis")
                synthesis_goal = Goal(id="synthesis", text=query, done=False)
                synth_attached: list[tuple[str, bytes]] = []
                for e in history:
                    aid = e.get("artifact_id")
                    if aid and artifacts.exists(aid):
                        synth_attached.append((aid, artifacts.get_bytes(aid)))
                synth_attached = synth_attached[-3:]
                out = decision.next_step(synthesis_goal, hits, synth_attached, history, [])
                if out.answer:
                    print(f"[decision] ANSWER: {out.answer[:200]}")
                    history.append({
                        "iter": it,
                        "kind": "answer",
                        "goal_id": "synthesis",
                        "text": out.answer,
                    })
                print("[done] all goals satisfied")
                break

            # ── select next unfinished goal ──
            goal = next((g for g in prior_goals if not g.done), None)
            if goal is None:
                break

            # ── attach artifact bytes if perception requested ──
            attached: list[tuple[str, bytes]] = []
            if goal.attach_artifact_id and artifacts.exists(goal.attach_artifact_id):
                aid = goal.attach_artifact_id
                attached.append((aid, artifacts.get_bytes(aid)))
                print(f"[attach] {aid} ({len(attached[0][1])} bytes)")

            # ── decision ──
            out = decision.next_step(goal, hits, attached, history, tools)

            if out.answer:
                print(f"[decision] ANSWER: {out.answer[:200]}")
                history.append({
                    "iter": it,
                    "kind": "answer",
                    "goal_id": goal.id,
                    "text": out.answer,
                })
                print()
                continue

            if out.tool_call:
                tc = out.tool_call
                print(f"[decision] TOOL_CALL: {tc.name}({json.dumps(tc.arguments)[:100]})")

                # ── action ──
                result_text, art_id = await action.execute(tc)

                # ── memory.record_outcome ──
                memory.record_outcome(
                    tool_name=tc.name,
                    tool_args=tc.arguments,
                    result_text=result_text,
                    artifact_id=art_id,
                    run_id=run_id,
                    goal_id=goal.id,
                )
                print("[memory.record_outcome]")

                history.append({
                    "iter": it,
                    "kind": "action",
                    "goal_id": goal.id,
                    "tool": tc.name,
                    "arguments": tc.arguments,
                    "result_descriptor": result_text[:2000],
                    "artifact_id": art_id,
                })
            else:
                print("[decision] ERROR: neither answer nor tool_call returned")
                break

            print()

    answer = final_answer_from(history)
    print(f"\nFINAL\n")
    print(answer)
    return answer


async def main():
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("\nEnter query: ").strip()
        if not query:
            print("No query provided.")
            return

    print()
    print("=" * 60)
    await run(query)


if __name__ == "__main__":
    asyncio.run(main())
