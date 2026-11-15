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
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# uv may not be on PATH when this script is spawned as a detached background
# process (same silent-failure class that hit session-end.py / pre-compact.py).
# Fall back to the known install location if `uv` isn't found on PATH.
UV_BIN = shutil.which("uv") or "/Users/christopherbole/.local/bin/uv"

from config import (
    COMPILE_FROM_SESSION_END,
    COMPILE_MIN_INTERVAL_HOURS,
    DAILY_DIR,
    VAULT_ROOT,
    FLUSH_DEDUP_SECONDS,
    FLUSH_LOG,
    FLUSH_STATE_FILE,
    REPO_ROOT,
    SCRIPTS_DIR,
    SDK_MODEL,
    STATE_FILE,
    log_to_inbox_master,
    notify_failure,
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


def commit_daily_note(log_path: Path) -> None:
    """Commit today's daily note immediately after writing to it.

    Why this exists, and why it is not optional:

    `.gitattributes` already gives `daily/YYYY-MM-DD.md` git's `union` merge
    driver, so two machines appending to the same day's note is a solved
    problem. But union merge only runs on TRACKED files. If this hook writes
    the note and nobody commits it, the other machine's copy arrives as
    "untracked working tree files would be overwritten by merge" and git
    refuses the pull outright, before any merge driver is consulted. Every
    pull after that fails until a human intervenes. On 2026-08-12 that left
    the Mac reading a full-day-stale vault with no error anywhere in a
    session, which is the exact silent-staleness failure vault-pull.sh was
    built to prevent.

    Committing here is what lets the union driver do its job.

    Deliberate choices:
      - Only this one path is staged and committed. Other sessions have work
        in the tree and none of it gets swept in.
      - Pre-commit hooks are NOT bypassed. The SSN/routing-number guard exists
        because it has caught real secrets; a blocked commit leaves the file
        untracked and the pull fails loudly, which is the safe direction.
      - No push. Committing is enough to unblock the merge, and pushing from a
        background hook invites its own races.
      - Never raises. A failure here must not take down the flush.
    """
    import subprocess as _sp

    # VAULT_ROOT, not REPO_ROOT. REPO_ROOT is this script's own repo
    # (~/AI-Workspace/memory-compiler); the daily note lives in the vault.
    def git(*args: str, **kw) -> _sp.CompletedProcess:
        return _sp.run(
            ["git", *args], cwd=str(VAULT_ROOT),
            capture_output=True, text=True, timeout=20, **kw
        )

    try:
        rel = str(log_path.relative_to(VAULT_ROOT))

        # Another writer holds the index. Leave it alone; the vault-pull.sh
        # guard sweeps up anything left untracked on the next session start.
        if (VAULT_ROOT / ".git" / "index.lock").exists():
            logging.info("commit_daily_note: index.lock present, skipping %s", rel)
            return

        if git("add", "--", rel).returncode != 0:
            return

        # Nothing staged for this path means the content was already committed.
        if git("diff", "--cached", "--quiet", "--", rel).returncode == 0:
            return

        host = os.uname().nodename.split(".")[0]
        res = git("commit", "-q", "-m", f"capture: daily note {log_path.stem} ({host})", "--", rel)
        if res.returncode != 0:
            # Most likely a pre-commit guard rejection. Unstage ONLY this path
            # so another session's staged work is untouched, and log why.
            git("restore", "--staged", "--", rel)
            logging.warning(
                "commit_daily_note: commit rejected for %s, left untracked. %s",
                rel, (res.stdout + res.stderr).strip()[:400],
            )
            return
        logging.info("commit_daily_note: committed %s", rel)
    except Exception as exc:  # never break the flush over a git failure
        logging.warning("commit_daily_note: %s", exc)


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
            f"# Daily {today.strftime('%Y-%m-%d')}\n\n"
            f"Append-only capture endpoint. Hook-captured session flushes below; "
            f"manual captures and /tldr slug files live alongside this in daily/.\n\n",
            encoding="utf-8",
        )

    # Provenance tag in a blockquote so it doesn't fight the content's own
    # `## Session Title: ...` H2. Content carries its own structure now.
    time_str = today.strftime("%H:%M")
    provenance = f"\n---\n\n> _auto-captured at {time_str} MST via {source}_\n\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(provenance)
        f.write(content)
        f.write("\n")

    commit_daily_note(log_path)


async def run_flush(context: str) -> str:
    """Claude Agent SDK call — whole-life extraction prompt."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    prompt = f"""You are writing the session TLDR + extracting what's worth preserving
from a Claude Code session Christopher just finished.

Christopher is the CEO of Honeybird Homes (real estate investment), is
building Buy Box AI, runs an AI consulting agency and a coaching program,
has a wife Bettina and three kids (a 7-year-old daughter, a 21-year-old
son, a 29-year-old son), is a man of deep faith, has ADHD, and uses
Claude Code for EVERY domain of his life — not just work.

A session might be about a deal, a Buy Box feature, a family moment, a
faith reflection, home stuff, personal admin, a brain dump of ideas, a
health check, a promise made to someone, a new person met, a shift in
how a buyer thinks about a property — anything. Do NOT assume a session
is about real estate or software engineering unless it actually is.

Write the output below in Christopher's voice ("Worked on X. Decided Y
because Z. Bettina said W."). First-person, tight, factual. Plain text,
no tools.

**Skip any section with nothing to report.** If the entire session is
trivial (routine lookups, mechanical file ops, ceremonial chatter) and
nothing below would have real content, respond with exactly: FLUSH_OK

---

## Session Title: [a 5–9 word title that names the session like a mini-TLDR headline, starting with a strong verb]

**TLDR:** [2–4 sentences in my voice — what this session was, what got accomplished, why it mattered. The narrative framing. Don't just list steps; tell the story.]

**What Got Shipped:**
- [Concrete artifacts created/updated/pushed — files, commits, specs, skills, drafts, deliverables. Include commit hashes, file paths, or names if they appear in the session. If nothing shipped, skip this section.]

**What I Decided:**
- [decisions made + why, in my voice]

**What I Learned / Realized:**
- [gotchas, patterns, insights, truths I surfaced]

**Promises / Commitments Made:**
- [anything I said I'd do for anyone — Bettina, kids, team, buyer, partner, myself]

**People Updates:**
- [new people mentioned; new info on existing peeps; relationship shifts]

**Deal / Business Shifts:**
- [specific deal status changes; buyer Buy Box criteria changes; institutional buyer intel; consulting pilot status; coaching curriculum decisions]

**Ideas / Brain Dumps:**
- [ideas I threw out — even half-formed; the "20 ideas so I don't lose 19" category]

**Faith / Family / Personal:**
- [reflections, scripture, prayers, kid moments, home projects, health, finances]

**Connections Noticed:**
- [cross-domain links I surfaced — "the X in Buy Box maps to the Y Pace Morby said" / "the pattern from a faith reflection mirrors how I coach agents"]

**Action Items:**
- [ ] [follow-ups and TODOs, with who/what/when if known]

**Where to Start Next:**
- [concrete forward-looking bullets for the next session — which file to open, which decision is pending, which person to contact, which unfinished thread to pick up. This is the handoff.]

**Files / Commits / Cross-Links Touched:**
- [explicit paths, commit hashes, repo URLs, peep files, skill names that came up. Helps `/pickup` rehydrate context later. Skip if none.]

**Repository / Codebase Activity:**
- [Archon workflow dispatches (workflow name, target repo, branch, outcome), PRs merged, notable commits, custom workflow tweaks, or repo-scoped lessons. Tag each entry with the repo slug from `~/second-brain/REPOSITORIES.md` (e.g. "claudeclaw: ...", "memory-compiler: ...", "Archon: ...") so reflection can route the entry to `~/second-brain/repositories/<slug>.md`. Skip if no codebase work happened.]

---

Rules:
- Skip sections that would be empty — don't pad.
- For trivial sessions, a short TLDR + Action Items is fine; everything else can be empty.
- For big sessions (shipping, multi-thread, major decisions), fill everything relevant.
- The Session Title + TLDR + Where to Start Next are what make this feel like a TLDR and not just a bullet dump. Do not skip those if there IS meaningful content.
- Skip: routine tool calls, file reads, mechanical ops, ceremonial chatter, generic boilerplate not specific to Christopher.

## Conversation Context

{context}
"""

    response = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                model=SDK_MODEL,
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
                if getattr(message, "is_error", False):
                    return (f"FLUSH_ERROR: result {getattr(message, 'subtype', 'error')}: "
                            f"{str(getattr(message, 'result', ''))[:200]}")
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
    if not COMPILE_FROM_SESSION_END:
        return  # nightly launchd run owns compile now (config.py, 2026-10-18)
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
    cmd = [UV_BIN, "run", "--directory", str(REPO_ROOT), "python", str(compile_script)]

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
    failed = False

    if response.startswith("FLUSH_ERROR"):
        # Checked first: an error string must never be read as FLUSH_OK or saved content.
        # Failure is an alert, not a ledger or daily-note entry (vault decay rules, 2026-09-06).
        failed = True
        logging.error("Result: %s", response)
        notify_failure(f"flush FAILED: {source} session {session_id[:8]} — {response[:120]}")
    elif "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK (nothing worth saving)")
        # Don't pollute daily note with empty flushes; just log to inbox-master.
        log_to_inbox_master(
            f"memory-compiler flush: {source} session {session_id[:8]} — FLUSH_OK (nothing extracted)"
        )
    elif not response.strip():
        failed = True
        logging.error("Result: empty response from Agent SDK")
        notify_failure(f"flush FAILED: {source} session {session_id[:8]} — empty response")
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, section=f"Memory Compiler — {source}", source=source)
        log_to_inbox_master(
            f"memory-compiler flush: {source} session {session_id[:8]} — {len(response)} chars appended to daily"
        )

    save_flush_state({"session_id": session_id, "timestamp": time.time()})
    context_file.unlink(missing_ok=True)
    maybe_trigger_compilation()
    if failed:
        logging.error("FAILED: flush for session %s did not save anything", session_id)
        print(f"FAILED: flush for session {session_id} (see {FLUSH_LOG})", file=sys.stderr)
        sys.exit(1)
    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
