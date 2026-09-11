"""Shared fixtures. Tests never touch the developer's real database or blob store."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel

from termguard.config import Settings
from termguard.storage import LocalObjectStore

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed entirely at a temp directory."""
    return Settings(
        storage_backend="local",
        blob_root=tmp_path / "blobs",
        db_url=f"sqlite:///{tmp_path / 'test.db'}",
        corpus_dir=REPO_ROOT / "data" / "corpus",
        out_dir=tmp_path / "out",
        rulebook_path=REPO_ROOT / "data" / "rulebook.yaml",
    )


@pytest.fixture
def store(settings: Settings) -> LocalObjectStore:
    return LocalObjectStore(settings.blob_root)


@pytest.fixture
def session(settings: Settings):
    """A session on a fresh, isolated database."""
    from termguard import db as db_module

    db_module.reset_engine()
    engine = db_module.init_db(settings)
    with Session(engine) as s:
        yield s
    db_module.reset_engine()


@pytest.fixture(scope="session")
def corpus_dir() -> Path:
    d = REPO_ROOT / "data" / "corpus"
    if not any(d.glob("*.docx")):
        pytest.skip("corpus not generated; run `make corpus`")
    return d
