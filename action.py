"""Action component — pure MCP tool dispatch, no LLM."""
from __future__ import annotations

import json

from mcp import ClientSession

from artifact_store import ArtifactStore
from models import ToolCallModel, INLINE_BUDGET


class Action:
    def __init__(self, session: ClientSession, artifact_store: ArtifactStore):
        self._session = session
        self._store = artifact_store

    async def execute(self, tool_call: ToolCallModel) -> tuple[str, str | None]:
        result = await self._session.call_tool(tool_call.name, arguments=tool_call.arguments)

        texts = []
        for content in result.content:
            if hasattr(content, "text"):
                texts.append(content.text)
            else:
                texts.append(str(content))

        full_text = "\n".join(texts)
        raw_bytes = full_text.encode("utf-8")

        if len(raw_bytes) > INLINE_BUDGET:
            descriptor = full_text[:2000]
            artifact_id = self._store.put(
                raw_bytes,
                content_type="text/plain",
                source=f"tool:{tool_call.name}",
                descriptor=descriptor,
            )
            meta = self._store.get_meta(artifact_id)
            preview = json.dumps(
                {"length_bytes": meta.size_bytes, "text": full_text[:200]},
                ensure_ascii=False,
            )
            print(
                f"[action] -> [artifact {artifact_id}, {meta.size_bytes} bytes] "
                f"preview: {preview}"
            )
            return descriptor, artifact_id

        print(f"[action]: {full_text[:300]}")
        return full_text, None
