"""Core data structures shared across the pipeline.

Keeping these in one place means the classifier, the workflow engine, the
audit log and the UI all speak the same language. The ActionRecord in
particular is deliberately dual-purpose: it is both the row we persist to
the audit log AND the line the operations team reads in the action summary.
One shape, two consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum


class RequestType(str, Enum):
    """The four request categories this hospital-operations intake handles.

    NB: this is a NON-CLINICAL operations desk. Anything that is a genuine
    medical emergency is out of scope and is routed straight to a human via
    the URGENT_SAFETY branch rather than auto-resolved.
    """

    FACILITY_EVS = "Facility / EVS request"
    PATIENT_COMPLAINT = "Patient experience complaint"
    GENERAL_ENQUIRY = "General enquiry"
    URGENT_SAFETY = "Urgent safety escalation"


class Urgency(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


@dataclass
class Classification:
    """Structured output of the AI classification step."""

    request_type: RequestType
    urgency: Urgency
    confidence: float          # 0.0 - 1.0, self-reported by the model
    sub_topic: str             # e.g. "parking", "spill cleanup", "billing"
    reasoning: str             # one-line justification, useful in the audit trail
    entities: dict = field(default_factory=dict)  # extracted location, requester, etc.

    def to_dict(self) -> dict:
        d = asdict(self)
        d["request_type"] = self.request_type.value
        d["urgency"] = self.urgency.value
        return d


@dataclass
class ActionRecord:
    """A single step executed by a remediation branch.

    This is the atom of the whole system: every branch produces an ordered
    list of these, they render as the action summary AND persist as audit rows.
    """

    step_name: str
    status: str                # "done", "flagged", "paused", "skipped"
    detail: str                # human-readable description of what happened
    artifact: str | None = None  # optional generated content (draft reply, alert text...)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessedRequest:
    """The full result of running one request through the pipeline."""

    request_id: str
    raw_text: str
    classification: Classification
    actions: list[ActionRecord] = field(default_factory=list)
    final_status: str = "open"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "raw_text": self.raw_text,
            "classification": self.classification.to_dict(),
            "actions": [a.to_dict() for a in self.actions],
            "final_status": self.final_status,
            "created_at": self.created_at,
        }
