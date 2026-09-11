"""Layer D: the HTTP surface.

Runs against a real pipeline execution over a two-document corpus, so the responses are
the shapes the dashboard actually receives.
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBSET = ("IFU-001.docx", "RMS-001.docx")


@pytest.fixture
def api(tmp_path: Path, corpus_dir: Path, monkeypatch):
    """A client whose settings point entirely at a temp directory."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in SUBSET:
        shutil.copy(corpus_dir / name, corpus / name)
    shutil.copy(corpus_dir / "ground_truth.json", corpus / "ground_truth.json")

    rulebook = tmp_path / "rulebook.yaml"
    shutil.copy(REPO_ROOT / "data" / "rulebook.yaml", rulebook)
    # The agent-authority policy is resolved next to the rulebook, so it has to travel
    # with it - without it the policy loads empty and delegates nothing, which is the
    # safe default but not what these tests are exercising.
    shutil.copy(REPO_ROOT / "data" / "policy.yaml", tmp_path / "policy.yaml")

    monkeypatch.setenv("TERMGUARD_DB_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("TERMGUARD_BLOB_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("TERMGUARD_CORPUS_DIR", str(corpus))
    monkeypatch.setenv("TERMGUARD_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("TERMGUARD_RULEBOOK", str(rulebook))

    from termguard import db as db_module

    db_module.reset_engine()
    from termguard.api import app

    with TestClient(app) as client:
        yield client
    db_module.reset_engine()


@pytest.fixture
def completed(api: TestClient) -> int:
    """A finished run, executed synchronously so tests are deterministic."""
    from termguard.config import get_settings
    from termguard.db import session_scope
    from termguard.pipeline import run_pipeline

    settings = get_settings()
    with session_scope(settings) as session:
        run, _ = run_pipeline(session, settings=settings, dry_run=True, actor="tester")
        return run.id


class TestHealth:
    def test_reports_its_configuration(self, api: TestClient) -> None:
        body = api.get("/health").json()
        assert body["status"] == "ok"
        assert body["storage"] == "local"
        assert body["database"] == "sqlite"

    def test_llm_is_off_by_default(self, api: TestClient) -> None:
        assert api.get("/health").json()["llm_live"] is False


class TestRuns:
    def test_summary_separates_mechanisms(self, api: TestClient, completed: int) -> None:
        """Constraint 2 has to be answerable straight from the API."""
        body = api.get(f"/runs/{completed}").json()
        mechanisms = body["changes"]["by_mechanism"]
        assert mechanisms["deterministic"] > 0
        assert mechanisms["ai"] > 0
        assert sum(mechanisms.values()) == body["changes"]["total"]

    def test_summary_counts_hits_by_part(self, api: TestClient, completed: int) -> None:
        by_part = api.get(f"/runs/{completed}").json()["hits"]["by_part"]
        assert by_part["body"] > 0
        assert by_part.get("header", 0) > 0
        assert by_part.get("footnote", 0) > 0

    def test_summary_lists_files_with_counts(self, api: TestClient, completed: int) -> None:
        files = api.get(f"/runs/{completed}").json()["files"]
        assert {f["name"] for f in files} == set(SUBSET)
        assert all(f["unambiguous"] + f["needs_judgment"] == f["hits"] for f in files)

    def test_records_the_rulebook_hash(self, api: TestClient, completed: int) -> None:
        from termguard.rulebook import load_rulebook

        from termguard.config import get_settings

        expected = load_rulebook(get_settings().rulebook_path).hash
        assert api.get(f"/runs/{completed}").json()["rulebook_hash"] == expected

    def test_latest_returns_the_most_recent(self, api: TestClient, completed: int) -> None:
        assert api.get("/runs/latest").json()["run_id"] == completed

    def test_latest_404s_before_any_run(self, api: TestClient) -> None:
        assert api.get("/runs/latest").status_code == 404

    def test_unknown_run_404s(self, api: TestClient) -> None:
        assert api.get("/runs/9999").status_code == 404


class TestHits:
    def test_filters_compose(self, api: TestClient, completed: int) -> None:
        body = api.get(
            f"/runs/{completed}/hits",
            params={"file": "IFU-001.docx", "classification": "needs_judgment"},
        ).json()
        assert body["hits"]
        assert all(h["file"] == "IFU-001.docx" for h in body["hits"])
        assert all(h["classification"] == "needs_judgment" for h in body["hits"])

    def test_filter_by_part(self, api: TestClient, completed: int) -> None:
        body = api.get(f"/runs/{completed}/hits", params={"part": "header"}).json()
        assert body["hits"] and all(h["part"] == "header" for h in body["hits"])

    def test_filter_by_rule(self, api: TestClient, completed: int) -> None:
        body = api.get(f"/runs/{completed}/hits", params={"rule": "R-001"}).json()
        assert body["hits"] and all(h["rule_id"] == "R-001" for h in body["hits"])

    def test_unknown_file_returns_empty(self, api: TestClient, completed: int) -> None:
        assert api.get(f"/runs/{completed}/hits", params={"file": "nope.docx"}).json()["total"] == 0

    def test_hits_carry_their_location_and_reason(self, api: TestClient, completed: int) -> None:
        hit = api.get(f"/runs/{completed}/hits").json()["hits"][0]
        assert hit["location"] and hit["reason"] and hit["sentence"]


class TestReviewQueue:
    def test_queue_items_carry_everything_needed_to_decide(self, api, completed: int) -> None:
        item = api.get(f"/runs/{completed}/queue").json()["items"][0]
        for field in ("rule_id", "original_text", "sentence", "paragraph_text",
                      "mechanism", "comment"):
            assert item[field] != "" and item[field] is not None

    def test_ai_items_show_their_model_and_justification(self, api, completed: int) -> None:
        body = api.get(f"/runs/{completed}/queue", params={"mechanism": "ai"}).json()
        assert body["items"]
        assert all(i["model"] for i in body["items"])
        assert all(i["justification"] for i in body["items"])

    def test_deciding_advances_the_queue(self, api: TestClient, completed: int) -> None:
        before = api.get(f"/runs/{completed}/queue").json()
        change_id = before["items"][0]["change_id"]

        response = api.post(
            f"/changes/{change_id}/decision",
            json={"decision": "accepted", "reviewer": "qa@meridian"},
        )
        assert response.status_code == 200
        assert response.json()["decision"] == "accepted"

        after = api.get(f"/runs/{completed}/queue").json()
        assert after["total"] == before["total"] - 1
        assert change_id not in {i["change_id"] for i in after["items"]}

    def test_edited_decision_requires_final_text(self, api: TestClient, completed: int) -> None:
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        response = api.post(
            f"/changes/{change_id}/decision",
            json={"decision": "edited", "reviewer": "qa@meridian"},
        )
        assert response.status_code == 400
        assert "final_text" in response.json()["detail"]

    def test_invalid_decision_value_is_rejected(self, api: TestClient, completed: int) -> None:
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        response = api.post(
            f"/changes/{change_id}/decision",
            json={"decision": "maybe", "reviewer": "qa@meridian"},
        )
        assert response.status_code == 400

    def test_decision_history_shows_supersession(self, api: TestClient, completed: int) -> None:
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        api.post(f"/changes/{change_id}/decision",
                 json={"decision": "accepted", "reviewer": "first@x"})
        api.post(f"/changes/{change_id}/decision",
                 json={"decision": "rejected", "reviewer": "second@x", "note": "overruled"})

        history = api.get(f"/changes/{change_id}/decisions").json()
        assert [h["decision"] for h in history] == ["accepted", "rejected"]
        assert history[0]["superseded"] is True
        assert history[1]["superseded"] is False

    def test_decided_filter_returns_only_decided(self, api: TestClient, completed: int) -> None:
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        api.post(f"/changes/{change_id}/decision",
                 json={"decision": "accepted", "reviewer": "qa@x"})
        decided = api.get(f"/runs/{completed}/queue", params={"status": "decided"}).json()
        assert [i["change_id"] for i in decided["items"]] == [change_id]


class TestVerification:
    def _accept_all(self, api: TestClient, run_id: int) -> None:
        while True:
            items = api.get(f"/runs/{run_id}/queue").json()["items"]
            if not items:
                return
            for item in items:
                api.post(f"/changes/{item['change_id']}/decision",
                         json={"decision": "accepted", "reviewer": "qa@meridian"})

    def test_undecided_changes_fail_the_gate(self, api: TestClient, completed: int) -> None:
        body = api.post(f"/runs/{completed}/verify").json()
        assert body["passed"] is False
        assert body["totals"]["undecided"] > 0

    def test_accepting_everything_passes(self, api: TestClient, completed: int) -> None:
        self._accept_all(api, completed)
        body = api.post(f"/runs/{completed}/verify").json()
        assert body["passed"] is True, body["files"]
        assert body["totals"]["remaining_hits"] == 0
        assert body["totals"]["unexplained_edits"] == 0

    def test_report_is_downloadable_as_markdown(self, api: TestClient, completed: int) -> None:
        self._accept_all(api, completed)
        api.post(f"/runs/{completed}/verify")
        text = api.get(f"/runs/{completed}/verification.md").text
        assert "**Result: PASS**" in text

    def test_report_404s_before_verification(self, api: TestClient, completed: int) -> None:
        assert api.get(f"/runs/{completed}/verification.md").status_code == 404


class TestDownloads:
    def test_redlined_copy_is_a_docx(self, api: TestClient, completed: int) -> None:
        response = api.get(f"/runs/{completed}/files/IFU-001.docx/redlined")
        assert response.status_code == 200
        assert response.content[:2] == b"PK"
        assert "wordprocessingml" in response.headers["content-type"]

    def test_final_copy_404s_before_verification(self, api: TestClient, completed: int) -> None:
        assert api.get(f"/runs/{completed}/files/IFU-001.docx/final").status_code == 404

    def test_unknown_file_404s(self, api: TestClient, completed: int) -> None:
        assert api.get(f"/runs/{completed}/files/nope.docx/redlined").status_code == 404


class TestAuditExport:
    def test_csv_has_one_row_per_change(self, api: TestClient, completed: int) -> None:
        response = api.get(f"/runs/{completed}/audit.csv")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]

        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == api.get(f"/runs/{completed}").json()["changes"]["total"]
        assert {r["mechanism"] for r in rows} == {"deterministic", "ai"}

    def test_event_log_is_readable(self, api: TestClient, completed: int) -> None:
        events = api.get(f"/runs/{completed}/events").json()
        kinds = {e["event"] for e in events}
        assert {"run.started", "document.scanned", "change.proposed", "run.completed"} <= kinds


class TestDocumentLifecycle:
    def test_documents_list_shows_version_counts(self, api: TestClient, completed: int) -> None:
        docs = api.get("/documents").json()
        assert {d["name"] for d in docs} == set(SUBSET)
        assert all(d["versions"] >= 1 for d in docs)
        assert all(len(d["current_sha256"]) == 64 for d in docs)

    def test_version_chain_is_ordered_and_linked(self, api: TestClient, completed: int) -> None:
        document_id = api.get("/documents").json()[0]["document_id"]
        versions = api.get(f"/documents/{document_id}/versions").json()
        assert [v["version_no"] for v in versions] == list(range(1, len(versions) + 1))
        assert versions[0]["stage"] == "ingested"
        assert versions[0]["parent_version_id"] is None
        for previous, current in zip(versions, versions[1:]):
            assert current["parent_version_id"] == previous["version_id"]

    def test_timeline_interleaves_versions_and_events(self, api: TestClient, completed: int) -> None:
        document_id = api.get("/documents").json()[0]["document_id"]
        timeline = api.get(f"/documents/{document_id}/timeline").json()
        assert {row["kind"] for row in timeline} == {"version", "event"}
        assert [r["at"] for r in timeline] == sorted(r["at"] for r in timeline)

    def test_any_past_version_can_be_downloaded_verbatim(
        self, api: TestClient, completed: int, corpus_dir: Path
    ) -> None:
        """The point of the version chain: reproduce any earlier state exactly."""
        document = next(d for d in api.get("/documents").json() if d["name"] == "IFU-001.docx")
        response = api.get(f"/documents/{document['document_id']}/versions/1/download")
        assert response.status_code == 200
        assert response.content == (corpus_dir / "IFU-001.docx").read_bytes()

    def test_redlined_version_differs_from_the_original(self, api: TestClient, completed: int) -> None:
        document = next(d for d in api.get("/documents").json() if d["name"] == "IFU-001.docx")
        first = api.get(f"/documents/{document['document_id']}/versions/1/download").content
        second = api.get(f"/documents/{document['document_id']}/versions/2/download").content
        assert first != second

    def test_unknown_version_404s(self, api: TestClient, completed: int) -> None:
        document_id = api.get("/documents").json()[0]["document_id"]
        assert api.get(f"/documents/{document_id}/versions/99/download").status_code == 404

    def test_integrity_check_passes_on_a_healthy_store(self, api: TestClient, completed: int) -> None:
        body = api.get("/integrity").json()
        assert body["ok"] is True
        assert body["checked"] > 0

    def test_integrity_check_detects_corruption(self, api: TestClient, completed: int) -> None:
        from termguard.config import get_settings
        from termguard.storage import blob_key

        root = get_settings().blob_root
        target = next(root.rglob("*.docx"))
        target.write_bytes(b"corrupted")

        body = api.get("/integrity").json()
        assert body["ok"] is False
        assert body["failures"]


class TestRulebookEndpoint:
    def test_read_returns_rules_and_hash(self, api: TestClient) -> None:
        body = api.get("/rulebook").json()
        assert len(body["rules"]) == 12
        assert len(body["hash"]) == 16
        assert body["modified_at"]

    def test_put_validates_before_writing(self, api: TestClient) -> None:
        before = api.get("/rulebook").json()
        response = api.put("/rulebook", json={"rules": [{"id": "NOPE", "deprecated": ["x"],
                                                         "approved": "y"}]})
        assert response.status_code == 400
        assert api.get("/rulebook").json()["hash"] == before["hash"]

    def test_put_bumps_the_hash_when_content_changes(self, api: TestClient) -> None:
        body = api.get("/rulebook").json()
        rules = body["rules"]
        rules[0]["approved"] = "Meridian Infusion Platform"

        response = api.put("/rulebook", json={"rules": rules, "version": body["version"]})
        assert response.status_code == 200
        payload = response.json()
        assert payload["changed"] is True
        assert payload["previous_hash"] == body["hash"]
        assert api.get("/rulebook").json()["hash"] == payload["hash"]

    def test_put_is_a_no_op_when_nothing_changes(self, api: TestClient) -> None:
        body = api.get("/rulebook").json()
        payload = api.put("/rulebook", json={"rules": body["rules"],
                                             "version": body["version"]}).json()
        assert payload["changed"] is False
        assert payload["hash"] == body["hash"]

    def test_put_rejects_a_rulebook_with_overlapping_terms(self, api: TestClient) -> None:
        body = api.get("/rulebook").json()
        rules = body["rules"]
        rules[1]["deprecated"] = rules[0]["deprecated"]
        assert api.put("/rulebook", json={"rules": rules}).status_code == 400


class TestMetricsEndpoint:
    def test_dashboard_returns_every_section(self, api: TestClient, completed: int) -> None:
        body = api.get("/metrics").json()
        assert {"posture", "ai_trust", "throughput", "rule_health", "attention"} <= set(body)
        assert body["run_id"] == completed

    def test_posture_counts_documents_and_versions(self, api: TestClient, completed: int) -> None:
        posture = api.get("/metrics").json()["posture"]
        assert posture["documents"] == len(SUBSET)
        assert posture["versions"] >= len(SUBSET)
        assert posture["changes"]["pending"] == posture["changes"]["total"]

    def test_attention_flags_pending_decisions(self, api: TestClient, completed: int) -> None:
        titles = " ".join(i["title"] for i in api.get("/metrics").json()["attention"])
        assert "awaiting a decision" in titles

    def test_rule_health_is_sorted_worst_first(self, api: TestClient, completed: int) -> None:
        rules = api.get("/metrics/rules").json()
        assert rules
        rates = [r["override_rate"] or 0 for r in rules]
        assert rates == sorted(rates, reverse=True)

    def test_acceptance_rate_is_none_before_any_verdict(self, api, completed: int) -> None:
        """No data and zero percent are different facts."""
        assert api.get("/metrics").json()["ai_trust"]["acceptance_rate"] is None

    def test_override_rate_reflects_a_rejection(self, api: TestClient, completed: int) -> None:
        ai_items = api.get(f"/runs/{completed}/queue", params={"mechanism": "ai"}).json()["items"]
        api.post(f"/changes/{ai_items[0]['change_id']}/decision",
                 json={"decision": "rejected", "reviewer": "qa@meridian"})
        api.post(f"/changes/{ai_items[1]['change_id']}/decision",
                 json={"decision": "accepted", "reviewer": "qa@meridian"})

        trust = api.get("/metrics").json()["ai_trust"]
        assert trust["judged_by_humans"] == 2
        assert trust["acceptance_rate"] == 0.5


class TestPolicyEndpoint:
    def test_policy_reports_clauses_and_hash(self, api: TestClient) -> None:
        body = api.get("/policy").json()
        assert len(body["hash"]) == 16
        assert body["clauses"]
        assert body["default"] == "human decides"

    def test_every_clause_explains_itself(self, api: TestClient) -> None:
        assert all(c["rationale"].strip() for c in api.get("/policy").json()["clauses"])

    def test_judgment_rules_are_not_delegated(self, api: TestClient) -> None:
        delegated = set(api.get("/policy").json()["rules_agents_may_decide"])
        assert not ({"R-002", "R-003", "R-010"} & delegated)

    def test_a_missing_policy_file_is_reported_as_missing(
        self, api: TestClient, tmp_path: Path
    ) -> None:
        """An absent policy and a policy granting nothing both delegate zero authority,
        but only one of them is a configuration mistake."""
        present = api.get("/policy").json()
        assert present["present"] is True

        (tmp_path / "policy.yaml").unlink()
        absent = api.get("/policy").json()
        assert absent["present"] is False
        assert absent["clauses"] == []
        assert absent["default"] == "human decides"


class TestParticipantsEndpoint:
    def test_create_and_list(self, api: TestClient) -> None:
        created = api.post("/participants", json={"name": "alice@x", "roles": ["reviewer"]})
        assert created.status_code == 201

        people = api.get("/participants").json()
        assert [p["name"] for p in people] == ["alice@x"]
        assert people[0]["kind"] == "human"

    def test_an_agent_records_its_model(self, api: TestClient) -> None:
        api.post("/participants", json={"name": "bot-1", "kind": "agent",
                                        "roles": ["reviewer"], "model": "claude-opus-5"})
        agent = next(p for p in api.get("/participants").json() if p["name"] == "bot-1")
        assert agent["kind"] == "agent" and agent["model"] == "claude-opus-5"

    def test_creation_is_idempotent(self, api: TestClient) -> None:
        api.post("/participants", json={"name": "dup@x", "roles": ["reviewer"]})
        api.post("/participants", json={"name": "dup@x", "roles": ["approver"]})
        people = [p for p in api.get("/participants").json() if p["name"] == "dup@x"]
        assert len(people) == 1
        assert set(people[0]["roles"]) == {"reviewer", "approver"}


class TestClaimsEndpoint:
    def _people(self, api: TestClient) -> None:
        for name in ("alice@x", "bob@x"):
            api.post("/participants", json={"name": name, "roles": ["reviewer"]})

    def test_a_second_claim_conflicts(self, api: TestClient, completed: int) -> None:
        self._people(api)
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]

        first = api.post(f"/changes/{change_id}/claim", json={"participant": "alice@x"})
        assert first.status_code == 200 and first.json()["expires_at"]

        second = api.post(f"/changes/{change_id}/claim", json={"participant": "bob@x"})
        assert second.status_code == 409
        assert "alice@x" in second.json()["detail"]

    def test_a_claim_blocks_another_reviewers_decision(self, api, completed: int) -> None:
        self._people(api)
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        api.post(f"/changes/{change_id}/claim", json={"participant": "alice@x"})

        blocked = api.post(f"/changes/{change_id}/decision",
                           json={"decision": "accepted", "reviewer": "bob@x"})
        assert blocked.status_code == 409

    def test_releasing_frees_the_change(self, api: TestClient, completed: int) -> None:
        self._people(api)
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        api.post(f"/changes/{change_id}/claim", json={"participant": "alice@x"})
        released = api.delete(f"/changes/{change_id}/claim", params={"participant": "alice@x"})
        assert released.json()["released"] is True
        assert api.post(f"/changes/{change_id}/claim",
                        json={"participant": "bob@x"}).status_code == 200

    def test_the_queue_shows_who_holds_a_change(self, api: TestClient, completed: int) -> None:
        self._people(api)
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        api.post(f"/changes/{change_id}/claim", json={"participant": "alice@x"})

        item = next(i for i in api.get(f"/runs/{completed}/queue").json()["items"]
                    if i["change_id"] == change_id)
        assert item["claimed_by"] == "alice@x"
        assert item["claim_expires_at"]

    def test_an_unknown_participant_is_rejected(self, api: TestClient, completed: int) -> None:
        change_id = api.get(f"/runs/{completed}/queue").json()["items"][0]["change_id"]
        response = api.post(f"/changes/{change_id}/claim", json={"participant": "ghost"})
        assert response.status_code == 400


class TestAgentEndpoints:
    def test_dry_run_previews_without_deciding(self, api: TestClient, completed: int) -> None:
        before = api.get(f"/runs/{completed}/queue").json()["total"]
        preview = api.post(f"/runs/{completed}/agent-dispose", json={"dry_run": True}).json()
        assert preview["decided"] > 0
        assert preview["left_to_humans"] > 0
        assert api.get(f"/runs/{completed}/queue").json()["total"] == before

    def test_agent_decisions_name_their_clause(self, api: TestClient, completed: int) -> None:
        result = api.post(f"/runs/{completed}/agent-dispose", json={}).json()
        assert result["by_clause"]
        assert result["policy_hash"]

        trust = api.get("/metrics").json()["ai_trust"]["agent_decided"]
        assert trust["count"] == result["decided"]
        assert trust["unattributed"] == 0

    def test_confirmation_clears_the_blocker(self, api: TestClient, completed: int) -> None:
        api.post("/participants", json={"name": "alice@x", "roles": ["reviewer"]})
        api.post(f"/runs/{completed}/agent-dispose", json={})

        readiness = api.get(f"/runs/{completed}/signoff").json()
        assert readiness["unconfirmed_agent_decisions"] > 0

        confirmed = api.post(f"/runs/{completed}/confirm-agent-batch",
                             json={"participant": "alice@x"}).json()
        assert confirmed["confirmed"] > 0
        assert api.get(f"/runs/{completed}/signoff").json()["unconfirmed_agent_decisions"] == 0


class TestAssignmentEndpoint:
    def test_work_is_spread_across_reviewers(self, api: TestClient, completed: int) -> None:
        for name in ("alice@x", "bob@x"):
            api.post("/participants", json={"name": name, "roles": ["reviewer"]})

        result = api.post(f"/runs/{completed}/assign",
                          json={"participants": ["alice@x", "bob@x"], "strategy": "by_file"}).json()
        assert len(result["assigned"]) == 2
        assert all(count > 0 for count in result["assigned"].values())

        item = api.get(f"/runs/{completed}/queue").json()["items"][0]
        assert item["assigned_to"] in {"alice@x", "bob@x"}

    def test_an_unknown_strategy_is_rejected(self, api: TestClient, completed: int) -> None:
        api.post("/participants", json={"name": "alice@x", "roles": ["reviewer"]})
        response = api.post(f"/runs/{completed}/assign",
                            json={"participants": ["alice@x"], "strategy": "vibes"})
        assert response.status_code == 400


class TestSignOffEndpoint:
    def _clear_and_verify(self, api: TestClient, run_id: int, reviewer: str) -> None:
        while True:
            items = api.get(f"/runs/{run_id}/queue").json()["items"]
            if not items:
                break
            for item in items:
                api.post(f"/changes/{item['change_id']}/decision",
                         json={"decision": "accepted", "reviewer": reviewer})
        api.post(f"/runs/{run_id}/verify")

    def test_readiness_lists_blockers(self, api: TestClient, completed: int) -> None:
        body = api.get(f"/runs/{completed}/signoff").json()
        assert body["ready"] is False
        assert body["undecided"] > 0

    def test_an_agent_can_never_sign_off(self, api: TestClient, completed: int) -> None:
        api.post("/participants", json={"name": "bot-1", "kind": "agent",
                                        "roles": ["reviewer", "approver"]})
        response = api.post(f"/runs/{completed}/signoff",
                            json={"participant": "bot-1", "decision": "approved"})
        assert response.status_code == 409
        assert "agent cannot sign off" in response.json()["detail"]

    def test_a_reviewer_of_the_run_cannot_approve_it(self, api: TestClient, completed: int) -> None:
        api.post("/participants", json={"name": "alice@x", "roles": ["reviewer", "approver"]})
        self._clear_and_verify(api, completed, "alice@x")

        response = api.post(f"/runs/{completed}/signoff",
                            json={"participant": "alice@x", "decision": "approved"})
        assert response.status_code == 409
        assert "maker-checker" in response.json()["detail"]

    def test_an_uninvolved_approver_can_sign(self, api: TestClient, completed: int) -> None:
        api.post("/participants", json={"name": "alice@x", "roles": ["reviewer"]})
        api.post("/participants", json={"name": "dana@x", "roles": ["approver"]})
        self._clear_and_verify(api, completed, "alice@x")

        readiness = api.get(f"/runs/{completed}/signoff").json()
        assert readiness["ready"] is True
        assert readiness["eligible_approvers"] == ["dana@x"]

        body = api.post(f"/runs/{completed}/signoff",
                        json={"participant": "dana@x", "decision": "approved",
                              "note": "reviewed"}).json()
        assert body["decision"] == "approved"
        assert body["rulebook_hash"] and body["policy_hash"]
        assert body["covered"]["decisions"] > 0

    def test_a_signed_run_appears_in_readiness(self, api: TestClient, completed: int) -> None:
        api.post("/participants", json={"name": "alice@x", "roles": ["reviewer"]})
        api.post("/participants", json={"name": "dana@x", "roles": ["approver"]})
        self._clear_and_verify(api, completed, "alice@x")
        api.post(f"/runs/{completed}/signoff",
                 json={"participant": "dana@x", "decision": "approved"})

        signed = api.get(f"/runs/{completed}/signoff").json()["signed"]
        assert len(signed) == 1 and signed[0]["participant"] == "dana@x"
