"""Shared utilities for the memory compiler — adapted for Christopher's vault."""

import hashlib
import json
import re
from pathlib import Path

from config import (
    CANDIDATES_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    STATE_FILE,
    VAULT_ROOT,
)


# ── State ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Wikilink helpers (Obsidian-style [[path/slug]]) ───────────────────

def extract_wikilinks(content: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def wiki_article_exists(link: str) -> bool:
    """Vault-aware: resolve link against compiler dirs AND the vault root.

    Christopher's vault has wikilinks like [[peeps/matt-reilly]], [[reference/tag-taxonomy]],
    [[honeybird/memory]]. A connection article can legitimately link anywhere in the vault.
    """
    link = link.split("|")[0].split("#")[0].strip()
    candidates = [
        KNOWLEDGE_DIR / f"{link}.md",
        KNOWLEDGE_DIR / link,
        CANDIDATES_DIR / f"{link}.md",
        VAULT_ROOT / f"{link}.md",
        VAULT_ROOT / link,
    ]
    return any(p.exists() for p in candidates)


# ── Wiki content ──────────────────────────────────────────────────────

def read_wiki_index() -> str:
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    # Christopher uses MEMORY.md as the curated index — this knowledge/index.md
    # is strictly the compiler's view into connections/candidates. Keep minimal.
    return (
        "# Memory Compiler Index (connections + candidates)\n\n"
        "This is the compiler-maintained catalog. For the curated memory index, "
        "see ~/.claude/projects/.../memory/MEMORY.md.\n\n"
        "| Article | Summary | Compiled From | Updated |\n"
        "|---------|---------|---------------|---------|"
    )


def read_all_wiki_content() -> str:
    """Read index + all connection articles + all candidate files."""
    parts = [f"## INDEX\n\n{read_wiki_index()}"]
    for subdir in [CONNECTIONS_DIR, CANDIDATES_DIR]:
        if not subdir.exists():
            continue
        for md_file in sorted(subdir.glob("*.md")):
            try:
                rel = md_file.relative_to(KNOWLEDGE_DIR)
            except ValueError:
                rel = md_file.name
            content = md_file.read_text(encoding="utf-8")
            parts.append(f"## {rel}\n\n{content}")
    return "\n\n---\n\n".join(parts)


def list_wiki_articles() -> list[Path]:
    """List all compiler-owned articles (connections + candidates)."""
    articles = []
    for subdir in [CONNECTIONS_DIR, CANDIDATES_DIR]:
        if subdir.exists():
            articles.extend(sorted(subdir.glob("*.md")))
    return articles


BARE_DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def list_raw_files() -> list[Path]:
    """List bare-date daily notes only (YYYY-MM-DD.md).

    Christopher's vault has two kinds of files in daily/:
      1. Bare-date files (YYYY-MM-DD.md) — the append-only hook capture endpoint
      2. Slug files (YYYY-MM-DD-topic.md) — his manual TLDRs and session notes

    The compiler only reads bare-date files. Slug files are his curated work product
    and should never be re-compiled (they're already compiled output from his side).
    """
    if not DAILY_DIR.exists():
        return []
    return sorted(
        p for p in DAILY_DIR.glob("*.md")
        if BARE_DAILY_RE.match(p.name)
    )


def list_slug_daily_files() -> list[Path]:
    """List slug-named daily files (YYYY-MM-DD-topic.md) — for lint only, not compile."""
    if not DAILY_DIR.exists():
        return []
    return sorted(
        p for p in DAILY_DIR.glob("*.md")
        if not BARE_DAILY_RE.match(p.name)
    )


def count_inbound_links(target: str, exclude_file: Path | None = None) -> int:
    """Count articles linking to target. Handles Obsidian aliased links.

    Fix for Cole's GH issue #7 tail: [[target|alias]] form was being missed.
    We now extract every wikilink, strip alias + heading fragments, and compare
    to the target.
    """
    count = 0
    target_norm = target.split("|")[0].split("#")[0].strip().replace("\\", "/")
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        content = article.read_text(encoding="utf-8")
        for link in extract_wikilinks(content):
            link_norm = link.split("|")[0].split("#")[0].strip().replace("\\", "/")
            if link_norm == target_norm:
                count += 1
                break
    return count


def get_article_word_count(path: Path) -> int:
    content = path.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:]
    return len(content.split())


def build_index_entry(rel_path: str, summary: str, sources: str, updated: str) -> str:
    link = rel_path.replace(".md", "")
    return f"| [[{link}]] | {summary} | {sources} | {updated} |"
