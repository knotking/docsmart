"""Layer D: what an agent is allowed to decide on its own.

Constraint 3 keeps the LLM's *edits* contained. This module contains its *authority*,
which is a different question: given a change the model proposed and validated, may an
agent also record the decision on it, or must a human?

The answer is data, not code. ``data/policy.yaml`` lists clauses, each naming the rules,
classifications and document parts an agent may dispose of, and every clause carries a
rationale. The file is hashed like the rulebook, and every agent decision records the
clause id and the policy hash that authorized it. So the question an auditor actually
asks - "on what authority did a machine approve this?" - has a specific answer: clause
P-001 of policy 3f2a9c, which says this, and here is who signed that policy off.

Anything no clause covers falls to a human. The default is not configurable, because a
policy that can grant blanket authority by omission is not a policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Risk(str, Enum):
    """How much damage a wrong decision on this change would do."""

    LOW = "low"          # orthography: spelling, units, hyphenation
    MEDIUM = "medium"    # terminology with a stable meaning
    HIGH = "high"        # anything context-dependent or in regulated quoted text

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class PolicyError(ValueError):
    """Raised when a policy file cannot be loaded or is internally inconsistent."""


class Clause(BaseModel):
    """One grant of authority. Absent a matching clause, a human decides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^P-\d{3}$")
    rules: list[str] = Field(default_factory=list, description="rule ids; empty means any")
    classifications: list[str] = Field(default_factory=lambda: ["unambiguous"])
    mechanisms: list[str] = Field(default_factory=lambda: ["deterministic"])
    parts: list[str] = Field(default_factory=list, description="document parts; empty means any")
    max_risk: Risk = Risk.LOW
    decision: str = Field(default="accepted", description="what the agent may record")
    requires_human_confirm: bool = False
    rationale: str = ""
    owner: str = ""

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, value: str) -> str:
        if value not in {"accepted", "rejected"}:
            raise ValueError("a clause may only authorize 'accepted' or 'rejected'")
        return value

    def covers(
        self,
        *,
        rule_id: str,
        classification: str,
        mechanism: str,
        part: str,
        risk: Risk,
    ) -> bool:
        """Whether this clause grants authority over a change with these properties."""
        if self.rules and rule_id not in self.rules:
            return False
        if self.classifications and classification not in self.classifications:
            return False
        if self.mechanisms and mechanism not in self.mechanisms:
            return False
        if self.parts and part not in self.parts:
            return False
        return risk.rank <= self.max_risk.rank


class Policy(BaseModel):
    """A validated, hashed set of clauses."""

    model_config = ConfigDict(frozen=True)

    clauses: list[Clause] = Field(default_factory=list)
    version: str = "1"
    risk_by_rule: dict[str, Risk] = Field(default_factory=dict)
    default_risk: Risk = Risk.HIGH
    approved_by: str = ""
    source_path: str | None = None
    hash: str = ""

    def __iter__(self) -> Iterator[Clause]:  # type: ignore[override]
        return iter(self.clauses)

    def __len__(self) -> int:
        return len(self.clauses)

    def get(self, clause_id: str) -> Clause:
        for clause in self.clauses:
            if clause.id == clause_id:
                return clause
        raise KeyError(f"no clause {clause_id!r}")

    def risk_of(self, rule_id: str) -> Risk:
        """A rule's risk band. Unlisted rules are HIGH, so a new rule is never auto-decided."""
        return self.risk_by_rule.get(rule_id, self.default_risk)

    def authorizes(
        self,
        *,
        rule_id: str,
        classification: str,
        mechanism: str,
        part: str,
        decision: str = "accepted",
    ) -> Clause | None:
        """The clause permitting an agent to record this decision, or None.

        None means a human decides. The first matching clause wins, so ordering in the
        file is meaningful and narrow clauses belong first.
        """
        risk = self.risk_of(rule_id)
        for clause in self.clauses:
            if clause.decision != decision:
                continue
            if clause.covers(
                rule_id=rule_id, classification=classification,
                mechanism=mechanism, part=part, risk=risk,
            ):
                return clause
        return None

    def summary(self) -> dict[str, Any]:
        """What this policy permits, in a form a person can read in a report."""
        return {
            "hash": self.hash,
            "version": self.version,
            "approved_by": self.approved_by,
            "clauses": len(self.clauses),
            "rules_agents_may_decide": sorted(
                {rule for clause in self.clauses for rule in clause.rules}
            ),
            "default": "human decides",
        }


def compute_hash(clauses: Sequence[Clause], risk_by_rule: dict[str, Risk]) -> str:
    """Content hash over the whole policy, including the risk table."""
    payload = json.dumps(
        {
            "clauses": [c.model_dump(mode="json") for c in clauses],
            "risk": {k: v.value for k, v in sorted(risk_by_rule.items())},
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_policy(path: Path | str) -> Policy:
    """Load, validate and hash an agent-authority policy."""
    path = Path(path)
    if not path.exists():
        # No policy file means no delegated authority at all - the safe reading.
        return Policy(clauses=[], version="0", hash=compute_hash([], {}))

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path}: expected a mapping at the top level")

    risk_by_rule = {
        rule_id: Risk(value)
        for rule_id, value in (raw.get("risk_by_rule") or {}).items()
    }

    clauses: list[Clause] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw.get("agent_may_decide") or []):
        if not isinstance(entry, dict):
            raise PolicyError(f"{path}: clause at position {index} is not a mapping")
        try:
            clause = Clause(**entry)
        except Exception as exc:
            raise PolicyError(f"{path}: clause {entry.get('id', index)} is invalid: {exc}") from exc
        if clause.id in seen:
            raise PolicyError(f"{path}: duplicate clause id {clause.id}")
        if not clause.rationale:
            raise PolicyError(
                f"{path}: clause {clause.id} has no rationale. A grant of authority to a "
                "machine has to say why it exists."
            )
        seen.add(clause.id)
        clauses.append(clause)

    return Policy(
        clauses=clauses,
        version=str(raw.get("version", "1")),
        risk_by_rule=risk_by_rule,
        default_risk=Risk(raw.get("default_risk", "high")),
        approved_by=str(raw.get("approved_by", "")),
        source_path=str(path),
        hash=compute_hash(clauses, risk_by_rule),
    )
