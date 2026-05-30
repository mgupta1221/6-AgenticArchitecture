"""Memory component — typed recall and durable record updates with vector search."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import faiss
import numpy as np

from models import MemoryItem, MemoryExtraction, MemoryItemDraft

STATE_FILE = Path(__file__).parent / "state" / "memory.json"
INDEX_FILE = Path(__file__).parent / "state" / "index.faiss"
IDS_FILE = Path(__file__).parent / "state" / "index_ids.json"
EMBED_DIM = 768


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 1}


class Memory:
    def __init__(self, llm):
        self._llm = llm
        self._items: list[MemoryItem] = []
        self._loaded = False

    # ── persistence ──

    def _load(self):
        if self._loaded:
            return
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self._items = [MemoryItem(**item) for item in data]
            except (json.JSONDecodeError, Exception):
                self._items = []
        self._loaded = True

    def _save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in self._items],
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def _persist_item(self, item: MemoryItem) -> MemoryItem:
        self._items.append(item)
        self._save()
        if item.embedding is not None:
            self._index_append(item.id, item.embedding)
        return item

    # ── FAISS index ──

    def _index_append(self, item_id: str, embedding: list[float]):
        if INDEX_FILE.exists() and IDS_FILE.exists():
            index = faiss.read_index(str(INDEX_FILE))
            ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
        else:
            index = faiss.IndexFlatIP(EMBED_DIM)
            ids = []
        vec = np.array([embedding], dtype=np.float32)
        faiss.normalize_L2(vec)
        index.add(vec)
        ids.append(item_id)
        faiss.write_index(index, str(INDEX_FILE))
        IDS_FILE.write_text(json.dumps(ids), encoding="utf-8")

    def _vector_search(self, query_embedding: list[float], top_k: int) -> list[str]:
        if not INDEX_FILE.exists() or not IDS_FILE.exists():
            return []
        index = faiss.read_index(str(INDEX_FILE))
        if index.ntotal == 0:
            return []
        ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
        vec = np.array([query_embedding], dtype=np.float32)
        faiss.normalize_L2(vec)
        k = min(top_k, index.ntotal)
        _, indices = index.search(vec, k)
        return [ids[i] for i in indices[0] if 0 <= i < len(ids)]

    # ── embedding ──

    def _try_embed(self, text: str, task_type: str = "retrieval_document") -> list[float] | None:
        try:
            result = self._llm.embed(text, task_type=task_type)
            embedding = result.get("embedding")
            if embedding and len(embedding) == EMBED_DIM:
                return embedding
        except Exception:
            pass
        return None

    # ── read methods ──

    def read(self, query: str, history: list[dict], kinds: list[str] | None = None, top_k: int = 8) -> list[MemoryItem]:
        self._load()

        query_emb = self._try_embed(query, task_type="retrieval_query")
        if query_emb is not None:
            hit_ids = self._vector_search(query_emb, top_k=top_k * 2)
            if hit_ids:
                id_to_item = {item.id: item for item in self._items}
                results = [id_to_item[hid] for hid in hit_ids if hid in id_to_item]
                if kinds:
                    results = [r for r in results if r.kind in kinds]
                results = results[:top_k]
                if results:
                    print(f"[memory.read] {len(results)} hits (vector)")
                    return results

        return self._keyword_read(query, history, kinds, top_k)

    def _keyword_read(self, query: str, history: list[dict], kinds: list[str] | None, top_k: int) -> list[MemoryItem]:
        query_tokens = _tokenize(query)
        for h in history[-5:]:
            query_tokens |= _tokenize(json.dumps(h, default=str)[:500])

        candidates = self._items
        if kinds:
            candidates = [item for item in candidates if item.kind in kinds]

        scored = []
        for item in candidates:
            item_tokens = set(item.keywords) | _tokenize(item.descriptor)
            overlap = len(query_tokens & item_tokens)
            if overlap > 0:
                scored.append((overlap, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [item for _, item in scored[:top_k]]
        print(f"[memory.read] {len(results)} hits (keyword fallback)")
        return results

    def filter(self, kinds: list[str] | None = None, goal_id: str | None = None, recent: int | None = None) -> list[MemoryItem]:
        self._load()
        items = list(self._items)
        if kinds:
            items = [i for i in items if i.kind in kinds]
        if goal_id:
            items = [i for i in items if i.goal_id == goal_id]
        if recent:
            items = items[-recent:]
        return items

    def relevant(self, query: str, kinds: list[str] | None = None, top_k: int = 5) -> list[MemoryItem]:
        self._load()
        candidates = self._items
        if kinds:
            candidates = [i for i in candidates if i.kind in kinds]
        if not candidates:
            return []

        items_text = "\n".join(
            f"[{i}] kind={it.kind} desc={it.descriptor}" for i, it in enumerate(candidates)
        )
        result = self._llm.chat(
            prompt=f"Query: {query}\n\nMemory items:\n{items_text}\n\nReturn the indices of the {top_k} most relevant items as a JSON list of integers.",
            system="You score memory items for relevance. Return only a JSON list of integer indices.",
            auto_route="memory",
            temperature=0.1,
            max_tokens=256,
        )
        try:
            indices = json.loads(result.get("text", "[]"))
            return [candidates[i] for i in indices if 0 <= i < len(candidates)][:top_k]
        except (json.JSONDecodeError, IndexError, TypeError):
            return candidates[:top_k]

    # ── write methods (LLM-powered) ──

    def remember(self, raw_text: str, source: str, run_id: str, goal_id: str | None = None) -> MemoryItem:
        self._load()
        schema = MemoryItemDraft.model_json_schema()
        result = self._llm.chat(
            prompt=f"Classify and extract structured information from this text:\n\n{raw_text}",
            system=(
                "You are a memory classifier. Given text, determine if it is a fact, preference, "
                "tool_outcome, or scratchpad note. Extract keywords (important nouns/verbs), "
                "a short descriptor (one sentence), and a structured value dict. Return JSON."
            ),
            provider="az",
            response_format={"type": "json_schema", "schema": schema, "name": "memory_item"},
            temperature=0.2,
            max_tokens=1024,
        )
        parsed = result.get("parsed") or json.loads(result["text"])

        embedding = None
        if parsed["kind"] != "scratchpad":
            embedding = self._try_embed(parsed["descriptor"])
            if embedding:
                print("[memory.embed]")

        item = MemoryItem(
            id=uuid.uuid4().hex[:8],
            kind=parsed["kind"],
            keywords=parsed["keywords"],
            descriptor=parsed["descriptor"],
            value=parsed["value"],
            embedding=embedding,
            source=source,
            run_id=run_id,
            goal_id=goal_id,
            confidence=0.8,
            created_at=datetime.now(timezone.utc),
        )
        return self._persist_item(item)

    def record_outcome(
        self,
        tool_call=None,
        tool_name: str = "",
        tool_args: dict | None = None,
        result_text: str = "",
        artifact_id: str | None = None,
        run_id: str = "",
        goal_id: str | None = None,
    ) -> list[MemoryItem]:
        if tool_call is not None:
            tool_name = tool_call.name
            tool_args = tool_call.arguments
        if tool_args is None:
            tool_args = {}
        self._load()
        schema = MemoryExtraction.model_json_schema()
        preview = result_text[:2000] if len(result_text) > 2000 else result_text
        prompt = (
            f"A tool was called and produced a result. Extract memory items from it.\n\n"
            f"Tool: {tool_name}\n"
            f"Arguments: {json.dumps(tool_args)}\n"
            f"Result preview: {preview}\n"
            f"Artifact ID: {artifact_id or 'none'}\n\n"
            f"Always create one tool_outcome entry summarizing what the tool returned. "
            f"Also extract any facts or preferences found in the result. "
            f"Keep keywords concise — important nouns and verbs only."
        )
        result = self._llm.chat(
            prompt=prompt,
            system=(
                "You are a memory classifier. Given a tool call and its result, create memory items. "
                "Always include one tool_outcome. Optionally extract facts or preferences. Return JSON."
            ),
            provider="az",
            response_format={"type": "json_schema", "schema": schema, "name": "memory_extraction"},
            temperature=0.2,
            max_tokens=1024,
        )
        parsed = result.get("parsed") or json.loads(result["text"])

        new_items = []
        for raw in parsed["items"]:
            embedding = None
            if raw["kind"] != "scratchpad":
                embedding = self._try_embed(raw["descriptor"])
                if embedding:
                    print("[memory.embed]")

            item = MemoryItem(
                id=uuid.uuid4().hex[:8],
                kind=raw["kind"],
                keywords=raw["keywords"],
                descriptor=raw["descriptor"],
                value=raw["value"],
                artifact_id=artifact_id,
                embedding=embedding,
                source=f"tool:{tool_name}",
                run_id=run_id,
                goal_id=goal_id,
                confidence=0.9,
                created_at=datetime.now(timezone.utc),
            )
            new_items.append(item)

        for item in new_items:
            self._persist_item(item)
        return new_items

    def add_fact(
        self,
        descriptor: str,
        *,
        value: dict,
        keywords: list[str],
        source: str,
        run_id: str,
        goal_id: str | None = None,
    ) -> MemoryItem:
        self._load()
        embedding = self._try_embed(descriptor)
        if embedding:
            print("[memory.embed]")
        item = MemoryItem(
            id=uuid.uuid4().hex[:8],
            kind="fact",
            keywords=[k.lower() for k in keywords],
            descriptor=descriptor,
            value=value,
            embedding=embedding,
            source=source,
            run_id=run_id,
            goal_id=goal_id,
            created_at=datetime.now(timezone.utc),
        )
        return self._persist_item(item)
