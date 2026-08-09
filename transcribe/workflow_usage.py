"""Recover per-agent token/duration usage from a completed Workflow
run's transcript directory.

Workflow's own on-disk records (journal.jsonl, agent-*.meta.json) do
NOT carry the label or per-agent token/duration breakdown that the
Claude Code UI shows live during a run (confirmed by inspection,
2026-08-09) -- that view is reconstructed from streamed progress
events, not persisted. What IS on disk is each agent's raw transcript
(agent-<id>.jsonl), and each assistant turn in it carries a real
`usage` block from the API.

Per this project's own prior finding (see CLAUDE.md history, the
"token double-counting bug"): usage fields like cache_read_input_tokens
are cumulative per-turn within a conversation. The correct total for
one agent is the LAST assistant turn's usage object alone, not a sum
across turns.

Page number and call kind (cleanup vs items) aren't stored as
structured fields either -- recovered from the first user message's
prompt text, which always contains "page N" (see ocr_llm.py's
CLEANUP_PROMPT_TEMPLATE / ITEMS_PROMPT_TEMPLATE) and the agent's own
agentType meta field distinguishes cleanup from items.
"""

from __future__ import annotations

import glob
import json
import os
import re

_PAGE_RE = re.compile(r"page (\d+)\b")


def _agent_ids_in_run(run_dir: str) -> list[str]:
    ids = []
    for path in glob.glob(os.path.join(run_dir, "agent-*.meta.json")):
        base = os.path.basename(path)
        ids.append(base[len("agent-"):-len(".meta.json")])
    return ids


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def extract_agent_usage(run_dir: str) -> list[dict]:
    """One dict per agent in the run:
    {agent_id, kind ('cleanup'|'items'|unknown), page, model,
     tokens_in, tokens_out, tool_calls, duration_ms}
    Skips agents whose transcript is missing or has no usage data
    (e.g. the run crashed before any turn completed) rather than
    guessing.
    """
    results = []
    for agent_id in _agent_ids_in_run(run_dir):
        meta_path = os.path.join(run_dir, f"agent-{agent_id}.meta.json")
        transcript_path = os.path.join(run_dir, f"agent-{agent_id}.jsonl")
        if not os.path.isfile(transcript_path):
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        agent_type = meta.get("agentType", "")
        kind = ("cleanup" if "cleanup" in agent_type
                else "items" if "items" in agent_type else "unknown")

        with open(transcript_path) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        if not entries:
            continue

        page = None
        for e in entries:
            if e.get("type") == "user":
                text = _extract_text(e.get("message", {}).get("content"))
                m = _PAGE_RE.search(text)
                if m:
                    page = int(m.group(1))
                    break

        assistant_msgs = [e for e in entries if e.get("type") == "assistant"]
        if not assistant_msgs:
            continue
        last_usage = assistant_msgs[-1]["message"].get("usage", {})
        tokens_in = (
            (last_usage.get("input_tokens") or 0)
            + (last_usage.get("cache_creation_input_tokens") or 0)
            + (last_usage.get("cache_read_input_tokens") or 0)
        )
        tokens_out = last_usage.get("output_tokens") or 0
        model = assistant_msgs[-1]["message"].get("model")

        tool_calls = 0
        for e in assistant_msgs:
            for block in e["message"].get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls += 1

        start_ts = entries[0].get("timestamp")
        end_ts = entries[-1].get("timestamp")
        duration_ms = None
        if start_ts and end_ts:
            from datetime import datetime
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
            try:
                duration_ms = int(
                    (datetime.strptime(end_ts, fmt) - datetime.strptime(start_ts, fmt))
                    .total_seconds() * 1000)
            except ValueError:
                duration_ms = None

        results.append({
            "agent_id": agent_id, "kind": kind, "page": page, "model": model,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tool_calls": tool_calls, "duration_ms": duration_ms,
        })
    return results
