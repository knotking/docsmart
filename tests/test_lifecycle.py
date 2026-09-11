"""Layer S: storage, version chain and audit trail."""

from __future__ import annotations

from pathlib import Path

import pytest

from termguard import audit, documents
from termguard.models import ActorKind, Stage
from termguard.storage import LocalObjectStore, blob_key, sha256_bytes


class TestObjectStore:
    def test_content_addressed_and_deduplicated(self, store: LocalObjectStore) -> None:
        d1 = store.put_bytes(b"identical")
        d2 = store.put_bytes(b"identical")
        assert d1 == d2 == sha256_bytes(b"identical")
        assert len(list(store.root.rglob("*.docx"))) == 1

    def test_roundtrip_and_verify(self, store: LocalObjectStore) -> None:
        digest = store.put_bytes(b"payload")
        assert store.get_bytes(digest) == b"payload"
        assert store.exists(digest)
        assert store.verify(digest)

    def test_missing_blob_raises(self, store: LocalObjectStore) -> None:
        with pytest.raises(KeyError):
            store.get_bytes("0" * 64)

    def test_tampering_is_detected(self, store: LocalObjectStore) -> None:
        digest = store.put_bytes(b"original")
        (store.root / blob_key(digest)).write_bytes(b"tampered")
        assert not store.verify(digest)

    def test_temp_copy_is_cleaned_up(self, store: LocalObjectStore) -> None:
        digest = store.put_bytes(b"data")
        with store.temp_copy(digest) as path:
            assert path.read_bytes() == b"data"
            captured = path
        assert not captured.exists()


class TestVersionChain:
    def test_ingest_creates_v1(self, session, store, tmp_path: Path) -> None:
        src = tmp_path / "IFU-001.docx"
        src.write_bytes(b"original content")
        doc, version = documents.ingest(session, store, src, actor="tester")

        assert doc.name == "IFU-001.docx"
        assert (version.version_no, version.stage) == (1, Stage.INGESTED)
        assert version.content_sha256 == sha256_bytes(b"original content")
        assert doc.current_version_id == version.id

    def test_ingest_is_idempotent(self, session, store, tmp_path: Path) -> None:
        src = tmp_path / "a.docx"
        src.write_bytes(b"same")
        _, v1 = documents.ingest(session, store, src, actor="t")
        _, v2 = documents.ingest(session, store, src, actor="t")
        assert v1.id == v2.id
        assert len(documents.history(session, v1.document_id)) == 1

    def test_full_chain_is_walkable(self, session, store, tmp_path: Path) -> None:
        src = tmp_path / "SOP-002.docx"
        src.write_bytes(b"v1 bytes")
        doc, v1 = documents.ingest(session, store, src, actor="ingestor")

        v2 = documents.add_version(
            session, store, doc, b"v2 redlined bytes", Stage.REDLINED,
            actor="TermGuard (rule engine)", actor_kind=ActorKind.RULE_ENGINE, parent=v1,
            summary={"deterministic": 4, "ai": 1},
        )
        v3 = documents.add_version(
            session, store, doc, b"v3 final bytes", Stage.FINAL,
            actor="reviewer@meridian", actor_kind=ActorKind.HUMAN, parent=v2,
        )

        assert [v.version_no for v in documents.history(session, doc.id)] == [1, 2, 3]
        assert [v.stage for v in documents.lineage(session, v3.id)] == [
            Stage.INGESTED, Stage.REDLINED, Stage.FINAL,
        ]
        assert documents.latest(session, doc.id).id == v3.id
        assert documents.latest_at_stage(session, doc.id, Stage.REDLINED).id == v2.id
        assert v2.summary == {"deterministic": 4, "ai": 1}

    def test_every_version_is_retrievable_verbatim(self, session, store, tmp_path: Path) -> None:
        """The point of the whole layer: any past state can be produced on demand."""
        src = tmp_path / "x.docx"
        src.write_bytes(b"state one")
        doc, v1 = documents.ingest(session, store, src, actor="t")
        v2 = documents.add_version(session, store, doc, b"state two", Stage.REDLINED,
                                   actor="t", parent=v1)
        v3 = documents.add_version(session, store, doc, b"state three", Stage.FINAL,
                                   actor="t", parent=v2)

        assert documents.content(store, v1) == b"state one"
        assert documents.content(store, v2) == b"state two"
        assert documents.content(store, v3) == b"state three"

    def test_unchanged_content_does_not_duplicate_a_version(self, session, store, tmp_path) -> None:
        src = tmp_path / "y.docx"
        src.write_bytes(b"unchanged")
        doc, v1 = documents.ingest(session, store, src, actor="t")
        a = documents.add_version(session, store, doc, b"same", Stage.REDLINED, actor="t", parent=v1)
        b = documents.add_version(session, store, doc, b"same", Stage.REDLINED, actor="t", parent=v1)
        assert a.id == b.id

    def test_no_op_stage_is_flagged_in_the_audit_trail(self, session, store, tmp_path) -> None:
        src = tmp_path / "z.docx"
        src.write_bytes(b"content")
        doc, v1 = documents.ingest(session, store, src, actor="t")
        documents.add_version(session, store, doc, b"content", Stage.REDLINED, actor="t", parent=v1)
        events = audit.events_for_document(session, doc.id)
        redline_event = [e for e in events if e.event == "version.redlined"][0]
        assert redline_event.payload["content_unchanged"] is True

    def test_integrity_check_catches_corruption(self, session, store, tmp_path: Path) -> None:
        src = tmp_path / "w.docx"
        src.write_bytes(b"trustworthy")
        doc, version = documents.ingest(session, store, src, actor="t")
        assert documents.verify_integrity(session, store, doc.id)["ok"] is True

        (store.root / blob_key(version.content_sha256)).write_bytes(b"corrupted")
        report = documents.verify_integrity(session, store, doc.id)
        assert report["ok"] is False
        assert report["failures"][0]["problem"] == "content does not match digest"


class TestAuditTrail:
    def test_every_version_emits_an_event(self, session, store, tmp_path: Path) -> None:
        src = tmp_path / "b.docx"
        src.write_bytes(b"one")
        doc, v1 = documents.ingest(session, store, src, actor="ingestor")
        documents.add_version(session, store, doc, b"two", Stage.REDLINED,
                              actor="rule engine", actor_kind=ActorKind.RULE_ENGINE, parent=v1)

        events = audit.events_for_document(session, doc.id)
        assert [e.event for e in events] == ["version.ingested", "version.redlined"]
        assert events[1].actor_kind == ActorKind.RULE_ENGINE
        assert events[0].content_sha256 == v1.content_sha256

    def test_timeline_is_chronological(self, session, store, tmp_path: Path) -> None:
        src = tmp_path / "c.docx"
        src.write_bytes(b"one")
        doc, v1 = documents.ingest(session, store, src, actor="t")
        documents.add_version(session, store, doc, b"two", Stage.REDLINED, actor="t", parent=v1)

        timeline = documents.document_timeline(session, doc.id)
        assert [r["at"] for r in timeline] == sorted(r["at"] for r in timeline)
        assert {r["kind"] for r in timeline} == {"version", "event"}
