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
import time

_PAGE_RE = re.compile(r"page (\d+)\b")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Path shape: ~/.claude/projects/<project>/<session-id>/subagents/
# workflows/wf_<run-id>/ -- two levels between "projects/" and
# "subagents/" (project dir, then session id), not scoped to this
# one project's session, since the agentType filter below (ocr-
# cleanup/ocr-items) is already an unambiguous match; no other
# workflow uses those two names.
_WORKFLOW_RUN_GLOB = os.path.expanduser(
    "~/.claude/projects/*/*/subagents/workflows/wf_*")

ACTIVE_RUN_MAX_AGE_S = 1800  # a run whose journal hasn't moved in 30min is stale, not active


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


def _page_and_date_from_transcript(run_dir: str, agent_id: str) -> tuple[int | None, str | None]:
    transcript_path = os.path.join(run_dir, f"agent-{agent_id}.jsonl")
    if not os.path.isfile(transcript_path):
        return None, None
    try:
        with open(transcript_path) as f:
            first_line = f.readline()
        entry = json.loads(first_line)
        text = _extract_text(entry.get("message", {}).get("content"))
        page_m = _PAGE_RE.search(text)
        date_m = _DATE_RE.search(text)
        return (int(page_m.group(1)) if page_m else None,
                date_m.group(1) if date_m else None)
    except (json.JSONDecodeError, OSError):
        return None, None


def find_active_runs(max_age_s: int = ACTIVE_RUN_MAX_AGE_S) -> list[dict]:
    """Live progress for any in-flight ocr-cleanup/ocr-items Workflow
    run, reconstructed from journal.jsonl + agent-*.meta.json +
    each agent's own transcript (for page/kind) -- no agent-side
    changes needed, since ocr-cleanup/ocr-items are deliberately
    Read-only and can't call a status-reporting script themselves.
    A run is "active" if its journal has moved within max_age_s;
    older runs are assumed already ingested (or abandoned) and are
    the DB's job to reflect, not this live-progress view's.
    """
    runs = []
    now = time.time()
    for run_dir in glob.glob(_WORKFLOW_RUN_GLOB):
        journal_path = os.path.join(run_dir, "journal.jsonl")
        if not os.path.isfile(journal_path):
            continue
        if now - os.path.getmtime(journal_path) > max_age_s:
            continue

        meta_by_id = {}
        for meta_path in glob.glob(os.path.join(run_dir, "agent-*.meta.json")):
            agent_id = os.path.basename(meta_path)[len("agent-"):-len(".meta.json")]
            with open(meta_path) as f:
                meta_by_id[agent_id] = json.load(f)
        if not any(m.get("agentType") in ("ocr-cleanup", "ocr-items")
                   for m in meta_by_id.values()):
            continue  # not an OCR+LLM run

        started_ids, result_ids = set(), set()
        with open(journal_path) as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                agent_id = entry.get("agentId")
                if entry.get("type") == "started":
                    started_ids.add(agent_id)
                elif entry.get("type") == "result":
                    result_ids.add(agent_id)

        pages = {}
        date = None
        for agent_id, meta in meta_by_id.items():
            agent_type = meta.get("agentType", "")
            kind = ("cleanup" if "cleanup" in agent_type
                    else "items" if "items" in agent_type else None)
            if kind is None:
                continue
            page, agent_date = _page_and_date_from_transcript(run_dir, agent_id)
            if agent_date and date is None:
                date = agent_date
            if page is None:
                continue
            status = ("done" if agent_id in result_ids
                       else "running" if agent_id in started_ids else "pending")
            pages.setdefault(page, {})[kind] = status

        runs.append({
            "run_dir": run_dir,
            "date": date,
            "total_agents": len(meta_by_id),
            "started": len(started_ids),
            "completed": len(result_ids),
            "pages": [
                {"page": p, "cleanup": s.get("cleanup", "pending"),
                 "items": s.get("items", "pending")}
                for p, s in sorted(pages.items())
            ],
        })
    return runs
