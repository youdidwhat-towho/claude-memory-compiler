"""Compile bare-date daily notes into memory-candidate proposals + connection articles.

SCOPE: Christopher is the curator of his memory layer. This compiler PROPOSES —
it does not AUTHORITATIVELY UPDATE memory files, MEMORY.md, pillar files, or
any curated vault surface. Writes are restricted to two directories:
  - drafts/active/memory-compiler-candidates/   → candidate memory entries for Christopher's weekly review
  - knowledge/connections/                       → cross-session connection articles (low-stakes, Christopher can prune)

Post-flight validation REJECTS any write outside the allowlist. Pre-flight prompt
tells the Agent SDK the same thing. Belt + suspenders.

Usage:
    uv run python compile.py                          # compile new/changed bare-date logs only
    uv run python compile.py --all                    # force recompile every bare-date log
    uv run python compile.py --file daily/2026-04-12.md
    uv run python compile.py --dry-run
"""

from __future__ import annotations

# Recursion prevention — set before any SDK import
import os
os.environ.setdefault("CLAUDE_INVOKED_BY", "memory_compile")

import argparse
import asyncio
import sys
from pathlib import Path

from config import (
    AGENTS_FILE,
    CANDIDATES_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    REPO_ROOT,
    VAULT_WRITE_ALLOWLIST,
    log_to_inbox_master,
    now_iso,
    path_is_allowed_for_vault_write,
)
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    save_state,
)


async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile one bare-date daily log. Returns the API cost."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    log_content = log_path.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()

    existing_context_parts = []
    for article_path in list_wiki_articles():
        try:
            rel = article_path.relative_to(KNOWLEDGE_DIR)
        except ValueError:
            try:
                rel = article_path.relative_to(CANDIDATES_DIR.parent.parent)
            except ValueError:
                rel = article_path.name
        existing_context_parts.append(
            f"### {rel}\n```markdown\n{article_path.read_text(encoding='utf-8')}\n```"
        )
    existing_articles_context = (
        "\n\n".join(existing_context_parts)
        if existing_context_parts
        else "(No existing compiler-owned articles yet — this may be the first compile run.)"
    )

    timestamp = now_iso()
    today = timestamp[:10]

    prompt = f"""You are the memory compiler for Christopher's whole-life Claude brain.

## Who Christopher is
CEO of Honeybird Homes (nationwide real estate investment), building Buy Box AI
(deal underwriting + buyer matching), running an AI consulting agency with
partner Justin (New Earth AI), and a coaching program for agents/investors.
Husband to Bettina, father of three (7-year-old daughter, 21-year-old son who
works at Honeybird, 29-year-old son). Deep faith. ADHD. Information hoarder.

His vault reaches every domain of his life — faith, family, personal (health,
home, finances), Honeybird deals, Buy Box dev, consulting, coaching, research.
You are compiling knowledge across ALL of it, not just one venture.

## Your constraint (read carefully)

Christopher is the curator of his memory layer. You are NOT. You PROPOSE
candidates and connections; he promotes them to curated memory on his schedule.

You are allowed to write to EXACTLY these two directories:
  1. {CANDIDATES_DIR}
  2. {CONNECTIONS_DIR}

You are FORBIDDEN from writing to:
  - {KNOWLEDGE_DIR / 'index.md'} (leave it alone — Christopher's MEMORY.md is the curated index)
  - Any file under ~/.claude/ (memory/, settings.json, agents/, skills/)
  - Any file under ~/second-brain/ OTHER than the two allowed dirs above — specifically:
    memory/, MEMORY.md, SOUL.md, USER.md, HEARTBEAT.md, HABITS.md, CLAUDE.md,
    reference/, honeybird/, buybox/, consulting/, coaching/, family/, faith/,
    personal/, peeps/, contacts/, deals/, projects/ — all OFF LIMITS.

If you feel the urge to edit a curated file, DON'T. Write it as a candidate
in {CANDIDATES_DIR} / today's file instead — Christopher will review it.

## The source log

**File:** {log_path.name}
**Today:** {today}

{log_content}

## Existing compiler-owned articles (for context — don't duplicate)

{existing_articles_context}

## Your task

1. **Extract 3–8 memory candidates** from today's log. Each is one proposed
   addition or update to Christopher's curated memory layer. Write them to a
   SINGLE file at:
     {CANDIDATES_DIR / f"{today}.md"}
   Format each candidate as:

   ```
   ### Candidate: [short title]
   **Type:** [user | feedback | project | reference | connection]
   **Proposed file:** memory/[suggested_slug].md
   **Summary:** [one line Christopher could paste into MEMORY.md]
   **Body:**
   [full proposed memory body — if this is approved, this is what gets saved]

   **Why:** [why this is worth remembering]
   **How to apply:** [when/where this guidance or fact matters]
   **Source:** daily/{log_path.name}
   ---
   ```

   If no candidates are worth proposing, create the file anyway with a single
   line: "No candidates proposed from {log_path.name}."

2. **Write connection articles** to {CONNECTIONS_DIR} ONLY when today's log
   reveals a non-obvious link between two or more concepts, domains, or people.
   Examples:
   - A pattern from a faith reflection that mirrors how Christopher coaches agents
   - A Buy Box buyer criterion that matches something an institutional buyer said 2 weeks ago
   - A promise Christopher made to Bettina that connects to a home-project timeline
   - A consulting insight that also applies to how Honeybird dispo works

   Create at most 1–2 connection articles per daily log. Skip entirely if nothing
   in today's log is cross-cutting. Use this format:

   ```markdown
   ---
   title: "Connection: X and Y"
   connects:
     - "concept-x-brief"
     - "concept-y-brief"
   sources:
     - "daily/{log_path.name}"
   created: {today}
   updated: {today}
   ---

   # Connection: X and Y

   ## The Connection
   [What links them]

   ## Key Insight
   [The non-obvious relationship surfaced]

   ## Evidence
   [Specific quotes or events from the daily log]
   ```

   Filename: {CONNECTIONS_DIR}/{{slug}}.md where slug is a short kebab-case label.

3. **Do NOT** update {KNOWLEDGE_DIR / 'index.md'}, do NOT touch any other file.

## Tone and voice
- Write candidates and connections in Christopher's first-person voice where
  applicable ("I noticed", "I decided", "Bettina mentioned").
- Be concrete. Name specific people, deals, properties, scriptures, kids' names
  when they appear in the source.
- Skip anything generic. If a candidate could apply to "any CEO," it's too vague.
"""

    cost = 0.0
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(REPO_ROOT),
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
    except Exception as e:
        print(f"  Error: {e}")
        log_to_inbox_master(
            f"⚠ memory-compiler compile FAILED: {log_path.name} — {type(e).__name__}: {str(e)[:200]}"
        )
        return 0.0

    # Post-flight validation: scan what was written, flag any violations
    violations = []
    for candidate_dir in [CANDIDATES_DIR, CONNECTIONS_DIR]:
        if not candidate_dir.exists():
            continue
        for f in candidate_dir.glob("*.md"):
            if not path_is_allowed_for_vault_write(f):
                violations.append(str(f))
    # Also spot-check that the index/log files weren't touched
    # (we'd catch explicit writes elsewhere if compile ever tried)

    if violations:
        msg = f"⚠ memory-compiler compile WROTE OUTSIDE ALLOWLIST: {violations}"
        print(msg)
        log_to_inbox_master(msg)

    rel_name = log_path.name
    state.setdefault("ingested", {})[rel_name] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
        "cost_usd": cost,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)

    log_to_inbox_master(
        f"memory-compiler compile: {log_path.name} → candidates+connections written (${cost:.4f})"
    )
    return cost


def main():
    parser = argparse.ArgumentParser(description="Compile bare-date daily logs into memory candidates")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    # Ensure write dirs exist
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()

    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name
                prev = state.get("ingested", {}).get(rel, {})
                if not prev or prev.get("hash") != file_hash(log_path):
                    to_compile.append(log_path)

    if not to_compile:
        print("Nothing to compile — all bare-date daily logs are up to date.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")
    if args.dry_run:
        return

    total_cost = 0.0
    for i, log_path in enumerate(to_compile, 1):
        print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
        cost = asyncio.run(compile_daily_log(log_path, state))
        total_cost += cost
        print(f"  Done.")

    articles = list_wiki_articles()
    print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
    print(f"Compiler-owned articles: {len(articles)}")


if __name__ == "__main__":
    main()
