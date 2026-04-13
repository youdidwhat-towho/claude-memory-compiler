"""Memory flush agent — whole-life extraction for Christopher's vault.

Spawned by session-end.py or pre-compact.py as a detached background process.
Reads the pre-extracted conversation context from a .md temp file, calls the
Claude Agent SDK to decide what's worth saving, and appends the result to
today's daily note in the vault.

The hook itself does NO API calls. This script is where the token spend happens.

Scope reminder: whole-life. Every Claude Code session, every domain — faith,
family, personal, all ventures. The prompt below is written accordingly.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention — set BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    COMPILE_MIN_INTERVAL_HOURS,
    DAILY_DIR,
    FLUSH_DEDUP_SECONDS,
    FLUSH_LOG,
    FLUSH_STATE_FILE,
    REPO_ROOT,
    SCRIPTS_DIR,
    STATE_FILE,
    log_to_inbox_master,
)

logging.basicConfig(
    filename=str(FLUSH_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if FLUSH_STATE_FILE.exists():
        try:
            return json.loads(FLUSH_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    FLUSH_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def append_to_daily_log(content: str, section: str, source: str) -> None:
    """Append content to today's daily note. Creates the file if missing.

    Christopher's convention (CLAUDE.md): daily/YYYY-MM-DD.md is THE capture
    endpoint — append-only, timestamped. Hook-captured content lands here in its
    own subsection so it never collides with manual captures or /tldr slug files.
    """
    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"---\n"
            f"tags: [daily, auto-capture]\n"
            f"date: {today.strftime('%Y-%m-%d')}\n"
            f"---\n\n"
            f"# Daily — {today.strftime('%Y-%m-%d')}\n\n"
            f"Append-only capture endpoint. Hook-captured session flushes below; "
            f"manual captures and /tldr slug files live alongside this in daily/.\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    header = f"\n## {section} — {time_str} MST (source: {source})\n\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(header)
        f.write(content)
        f.write("\n")


async def run_flush(context: str) -> str:
    """Claude Agent SDK call — whole-life extraction prompt."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    prompt = f"""You are extracting what's worth preserving from a Claude Code session
Christopher just finished. Christopher is the CEO of Honeybird Homes (real
estate investment), is building Buy Box AI, runs an AI consulting agency and a
coaching program, has a wife Bettina and three kids (a 7-year-old daughter, a
21-year-old son, a 29-year-old son), is a man of deep faith, has ADHD, and
uses Claude Code for EVERY domain of his life — not just work.

A session might be about a deal, a Buy Box feature, a family moment, a faith
reflection, home stuff, personal admin, a brain dump of ideas, a health check,
a promise made to someone, a new person met, a shift in how a buyer thinks
about a property — anything. Do NOT assume a session is about real estate or
software engineering unless it actually is.

Return a concise structured extract of what's worth preserving. Write in
Christopher's voice ("Worked on X. Decided Y because Z. Bettina said W.").
First-person, tight, factual. No tools — plain text only.

Use ONLY the sections below that actually have content. Skip any section with
nothing to report. If nothing in the session is worth saving, respond with
exactly: FLUSH_OK

---

**Context:** [one line — what was this session about, plainly, no jargon]

**What I Decided:**
- [decisions made + why, in my voice]

**What I Learned / Realized:**
- [gotchas, patterns, insights, truths I surfaced]

**Promises / Commitments Made:**
- [anything I said I'd do for anyone — Bettina, kids, team, buyer, partner, myself]

**People Updates:**
- [new people mentioned; new info on existing peeps; relationship shifts; "Matt said X", "J'Lien is at stage Y"]

**Deal / Business Shifts:**
- [specific deal status changes; buyer Buy Box criteria changes; new institutional buyer intel; consulting pilot status; coaching curriculum decisions]

**Ideas / Brain Dumps:**
- [ideas I threw out — even half-formed; things to revisit; the "20 ideas so I don't lose 19" category]

**Faith / Family / Personal:**
- [reflections, scripture, prayers, kid moments, home projects, health, finances, anything personal]

**Action Items:**
- [ ] [follow-ups and TODOs, with who/what/when if known]

**Connections Noticed:**
- [cross-domain links I surfaced during the session — "the X in Buy Box maps to the Y Pace Morby said" / "the pattern from a faith reflection mirrors how I coach agents"]

---

Skip anything that is:
- Routine tool calls, file reads, or mechanical operations
- Trivial clarification or ceremonial back-and-forth
- Generic boilerplate that isn't Christopher-specific

## Conversation Context

{context}
"""

    response = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(REPO_ROOT),
                allowed_tools=[],
                max_turns=2,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    return response


def maybe_trigger_compilation() -> None:
    """Trigger compile.py if there's work to do AND enough time has passed.

    Replaces Cole's 6PM wall-clock gate (GH issues #4/#6) — Christopher doesn't
    keep 9-5 hours, and time-of-day is a brittle trigger anyway. Two gates:

    1. Find work: any bare-date daily that's uncompiled OR whose hash changed
    2. Check cadence: last compile run must be >= COMPILE_MIN_INTERVAL_HOURS ago

    No dollar cost gate — Christopher is on Max, no dollar-denominated usage to
    cap. The cadence gate is the only throttle and it's there to prevent
    thrashing (e.g., rapid sequential session ends), not to ration spending.
    """
    import subprocess as _sp
    from hashlib import sha256

    now = datetime.now(timezone.utc).astimezone()

    # Load compile state
    compile_state: dict = {}
    if STATE_FILE.exists():
        try:
            compile_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    ingested = compile_state.get("ingested", {})

    # 1. Is there work to do?
    work_exists = False
    bare_date_re = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    for p in DAILY_DIR.glob("*.md"):
        if not bare_date_re.match(p.name):
            continue
        rec = ingested.get(p.name)
        if not rec:
            work_exists = True
            break
        current_hash = sha256(p.read_bytes()).hexdigest()[:16]
        if rec.get("hash") != current_hash:
            work_exists = True
            break
    if not work_exists:
        return

    # 2. Cadence guard — don't thrash compile on rapid sequential session ends.
    last_compile_iso = None
    for rec in ingested.values():
        ts = rec.get("compiled_at")
        if ts and (last_compile_iso is None or ts > last_compile_iso):
            last_compile_iso = ts
    if last_compile_iso:
        try:
            last_compile_dt = datetime.fromisoformat(last_compile_iso)
            hours_since = (now - last_compile_dt).total_seconds() / 3600
            if hours_since < COMPILE_MIN_INTERVAL_HOURS:
                logging.info("SKIP compile: %.2fh since last (min %.2fh)",
                             hours_since, COMPILE_MIN_INTERVAL_HOURS)
                return
        except (ValueError, TypeError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("Compile triggered (work present, cadence OK)")
    cmd = ["uv", "run", "--directory", str(REPO_ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(REPO_ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


def main():
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id> [source]", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]
    source = sys.argv[3] if len(sys.argv) > 3 else "SessionEnd"

    logging.info("flush.py started (session=%s source=%s context=%s)",
                 session_id, source, context_file)

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        log_to_inbox_master(f"⚠ memory-compiler flush: context file missing ({context_file.name})")
        return

    state = load_flush_state()
    if (
        state.get("session_id") == session_id
        and time.time() - state.get("timestamp", 0) < FLUSH_DEDUP_SECONDS
    ):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return

    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars", session_id, len(context))

    response = asyncio.run(run_flush(context))

    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK (nothing worth saving)")
        # Don't pollute daily note with empty flushes; just log to inbox-master.
        log_to_inbox_master(
            f"memory-compiler flush: {source} session {session_id[:8]} — FLUSH_OK (nothing extracted)"
        )
    elif "FLUSH_ERROR" in response:
        logging.error("Result: %s", response)
        append_to_daily_log(response, section=f"Memory Compiler — {source} ERROR", source=source)
        log_to_inbox_master(
            f"⚠ memory-compiler flush FAILED: {source} session {session_id[:8]} — {response[:200]}"
        )
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, section=f"Memory Compiler — {source}", source=source)
        log_to_inbox_master(
            f"memory-compiler flush: {source} session {session_id[:8]} — {len(response)} chars appended to daily"
        )

    save_flush_state({"session_id": session_id, "timestamp": time.time()})
    context_file.unlink(missing_ok=True)
    maybe_trigger_compilation()
    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
