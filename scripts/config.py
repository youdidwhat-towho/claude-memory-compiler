"""Path constants and configuration — adapted for Christopher's whole-life vault.

IMPORTANT: This is a whole-life Claude brain — faith, family, personal, every venture.
NOT a venture-scoped tool. The hook fires on every Claude Code session regardless of
domain and writes to the vault at ~/second-brain/.

See projects/second-brain-upgrades/09-memory-compiler-evaluation.md for the full memo.
"""

from pathlib import Path
from datetime import datetime, timezone

# ── Vault is the data layer ───────────────────────────────────────────
# Scripts live in ~/AI-Workspace/memory-compiler/ (this repo).
# Data lives in ~/second-brain/ (Christopher's vault). Never confuse the two.
VAULT_ROOT = Path.home() / "second-brain"
REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Read-only source layer (vault) ────────────────────────────────────
DAILY_DIR = VAULT_ROOT / "daily"
INBOX_MASTER = VAULT_ROOT / "inbox" / "_inbox-master.md"

# ── Scoped write targets (vault, drafts + connections only) ───────────
# Compile.py is ALLOWED to write here and NOWHERE ELSE in the vault.
# Never write to memory/, MEMORY.md, SOUL.md, USER.md, HEARTBEAT.md, HABITS.md,
# CLAUDE.md, or any other curated layer.
CANDIDATES_DIR = VAULT_ROOT / "drafts" / "active" / "memory-compiler-candidates"
KNOWLEDGE_DIR = VAULT_ROOT / "knowledge"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
REPORTS_DIR = VAULT_ROOT / "outputs" / "lint-reports"

# Paths compile.py is allowed to write to (post-flight validation guard)
VAULT_WRITE_ALLOWLIST = (CANDIDATES_DIR, CONNECTIONS_DIR, REPORTS_DIR)

# ── Repo-local state (not in vault) ───────────────────────────────────
SCRIPTS_DIR = REPO_ROOT / "scripts"
HOOKS_DIR = REPO_ROOT / "hooks"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"
STATE_FILE = SCRIPTS_DIR / "state.json"
FLUSH_STATE_FILE = SCRIPTS_DIR / "last-flush.json"
FLUSH_LOG = SCRIPTS_DIR / "flush.log"
COMPILE_LOG = SCRIPTS_DIR / "compile.log"

INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LOG_FILE = KNOWLEDGE_DIR / "log.md"

# ── Timezone — Christopher is MST year-round, no DST ──────────────────
TIMEZONE = "America/Phoenix"

# ── Tunables ──────────────────────────────────────────────────────────
# Compile trigger policy (replaces Cole's 6PM wall-clock gate, see GH issues #4/#6).
# Compile fires when BOTH are true:
#   1. There's something to compile (uncompiled bare-date daily OR today's hash changed)
#   2. It's been >= COMPILE_MIN_INTERVAL_HOURS since the last compile run
# Time-of-day plays no role. Works regardless of when Christopher wraps up.
COMPILE_MIN_INTERVAL_HOURS = 1.0

# Cost cap — hard stop if today's compile spend exceeds this. Protects against
# runaway cost (Cole's GH issue #3: ellismw burned $115 in 20 min).
# Loud-fails to inbox/_inbox-master.md with ⚠ prefix when hit.
DAILY_COMPILE_COST_CAP_USD = 3.00

MAX_CONTEXT_CHARS = 15_000
MIN_TURNS_TO_FLUSH = 1
FLUSH_DEDUP_SECONDS = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def log_to_inbox_master(line: str) -> None:
    """Append a line to inbox/_inbox-master.md. Primary health signal for hook ops."""
    if not INBOX_MASTER.exists():
        return
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M MST")
    with open(INBOX_MASTER, "a", encoding="utf-8") as f:
        f.write(f"\n- {ts} — {line}\n")


def path_is_allowed_for_vault_write(path: Path) -> bool:
    """Post-flight guard for compile.py: only allow writes under allowlisted dirs."""
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    for allowed in VAULT_WRITE_ALLOWLIST:
        try:
            resolved.relative_to(allowed.resolve())
            return True
        except ValueError:
            continue
    return False
