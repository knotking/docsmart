"""Layer S (substrate): runtime configuration.

Everything that differs between the local demo and a GCP deployment is resolved here,
from environment variables. No other module may read ``os.environ`` for deployment
concerns. This is what makes "move it to GCP" a configuration change:

    local   TERMGUARD_STORAGE=local  TERMGUARD_DB_URL=sqlite:///termguard.db
    gcp     TERMGUARD_STORAGE=gcs    TERMGUARD_DB_URL=postgresql+psycopg://...
            TERMGUARD_GCS_BUCKET=termguard-docs
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

StorageBackend = Literal["local", "gcs"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings. Construct via :func:`get_settings`."""

    # --- storage -----------------------------------------------------------
    storage_backend: StorageBackend = "local"
    blob_root: Path = field(default_factory=lambda: REPO_ROOT / "data" / "blobs")
    gcs_bucket: str = ""
    gcs_prefix: str = "termguard"

    # --- database ----------------------------------------------------------
    db_url: str = f"sqlite:///{REPO_ROOT / 'termguard.db'}"

    # --- data locations ----------------------------------------------------
    corpus_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "corpus")
    out_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "out")
    rulebook_path: Path = field(default_factory=lambda: REPO_ROOT / "data" / "rulebook.yaml")

    # --- LLM ---------------------------------------------------------------
    anthropic_model: str = "claude-opus-5"
    llm_live: bool = False
    llm_max_tokens: int = 4096
    llm_effort: str = "low"
    fixture_dir: Path = field(default_factory=lambda: REPO_ROOT / "tests" / "fixtures" / "judge")

    # --- identity ----------------------------------------------------------
    rule_engine_author: str = "TermGuard (rule engine)"
    ai_author: str = "TermGuard (AI-proposed)"

    @property
    def redlined_dir(self) -> Path:
        return self.out_dir / "redlined"

    @property
    def final_dir(self) -> Path:
        return self.out_dir / "final"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")


def get_settings() -> Settings:
    """Build settings from the environment. Cheap; call it freely."""
    return Settings(
        storage_backend=_env("TERMGUARD_STORAGE", "local"),  # type: ignore[arg-type]
        blob_root=Path(_env("TERMGUARD_BLOB_ROOT", str(REPO_ROOT / "data" / "blobs"))),
        gcs_bucket=_env("TERMGUARD_GCS_BUCKET", ""),
        gcs_prefix=_env("TERMGUARD_GCS_PREFIX", "termguard"),
        db_url=_env("TERMGUARD_DB_URL", f"sqlite:///{REPO_ROOT / 'termguard.db'}"),
        corpus_dir=Path(_env("TERMGUARD_CORPUS_DIR", str(REPO_ROOT / "data" / "corpus"))),
        out_dir=Path(_env("TERMGUARD_OUT_DIR", str(REPO_ROOT / "data" / "out"))),
        rulebook_path=Path(_env("TERMGUARD_RULEBOOK", str(REPO_ROOT / "data" / "rulebook.yaml"))),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5"),
        llm_live=_flag("TERMGUARD_LLM_LIVE", False),
    )
