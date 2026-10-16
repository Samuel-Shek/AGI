from __future__ import annotations

from pathlib import Path


OBSIDIAN_VAULT_RELATIVE_PATH = (
    Path("Library")
    / "Mobile Documents"
    / "iCloud~md~obsidian"
    / "Documents"
    / "默认仓库"
)
AI_WORKBENCH_RELATIVE_PATH = Path("✦ AI 工作台")
FROZEN_PAM_LEDGER_FILENAME = "PAM 长期记忆台账（已停更）.md"

# Historical locations are discovery-only.  Callers must never create or
# mutate these paths; the PAM ledger has been retired in favour of mem0.
LEGACY_PAM_LEDGER_RELATIVE_PATHS = (
    OBSIDIAN_VAULT_RELATIVE_PATH / AI_WORKBENCH_RELATIVE_PATH / "PAM 长期记忆台账.md",
    Path("Documents") / "Obsidian Vault" / AI_WORKBENCH_RELATIVE_PATH / "PAM 长期记忆台账.md",
    Path("Documents") / "Obsidian Vault" / AI_WORKBENCH_RELATIVE_PATH / FROZEN_PAM_LEDGER_FILENAME,
)


def obsidian_vault_root(*, home: str | Path | None = None) -> Path:
    """Return the configured iCloud Obsidian vault without scanning $HOME."""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / OBSIDIAN_VAULT_RELATIVE_PATH


def frozen_pam_ledger_path(*, home: str | Path | None = None) -> Path:
    """Return the canonical, permanently read-only PAM ledger path."""
    return obsidian_vault_root(home=home) / AI_WORKBENCH_RELATIVE_PATH / FROZEN_PAM_LEDGER_FILENAME


def discover_frozen_pam_ledger_paths(*, home: str | Path | None = None) -> list[Path]:
    """Return existing canonical/legacy ledger files for read-only inspection."""
    root = Path(home).expanduser() if home is not None else Path.home()
    candidates = [
        frozen_pam_ledger_path(home=root),
        *(root / relative for relative in LEGACY_PAM_LEDGER_RELATIVE_PATHS),
    ]
    output: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        output.append(candidate)
    return output
