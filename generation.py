"""Text generation for remediation steps.

Every drafted artifact (acknowledgements, KB-grounded replies, confirmations)
comes through here. Live mode uses Sonnet -- a better writer than Haiku, which
matters for patient-facing tone. Mock mode uses templates so the full pipeline
runs deterministically with no API key.

Design note: generation is deliberately separate from classification. Different
models, different prompts, different failure modes -- and a reviewer can see at
a glance which model does which job and why.
"""

from __future__ import annotations

from pathlib import Path

from config import GENERATION_MODEL
from models import Classification

KB_PATH = Path(__file__).parent / "knowledge_base.md"

BASE_STYLE = (
    "You draft messages for a hospital's NON-CLINICAL operations desk. "
    "Warm, plain, professional English. Never give medical advice. Never "
    "promise clinical outcomes. Keep it under 150 words. Output ONLY the "
    "message body -- no subject line, no preamble, no signature block."
)


def _generate(client, system: str, user: str) -> str:
    resp = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


# ---------------------------------------------------------------------------
# Live generators
# ---------------------------------------------------------------------------

def draft_complaint_ack(text: str, c: Classification, client) -> str:
    return _generate(
        client,
        BASE_STYLE + " This is an acknowledgement of a patient experience "
        "complaint. Be genuinely empathetic without being defensive or "
        "admitting fault. State that it has been escalated to the Patient "
        "Liaison Office and that they will be contacted within 2 hours.",
        f"Complaint received:\n\n{text}",
    )


KB_REPLY_TOOL = {
    "name": "kb_reply",
    "description": "Return the drafted reply and whether it was answered from the knowledge base. Always call this tool.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "The message body to send to the enquirer.",
            },
            "answered_from_kb": {
                "type": "boolean",
                "description": (
                    "true if the enquiry was fully answered using the knowledge "
                    "base; false if any part of it could not be answered and "
                    "needs to be passed to the information desk."
                ),
            },
        },
        "required": ["reply", "answered_from_kb"],
    },
}


def draft_kb_reply(text: str, c: Classification, client) -> tuple[str, bool]:
    """Returns (reply_text, answered_from_kb).

    Structured tool use, same rationale as the classifier: whether the model
    answered or punted is a workflow-routing decision, so it must come back as
    a machine-readable boolean -- not as prose we'd have to keyword-sniff.
    """
    kb = KB_PATH.read_text(encoding="utf-8")
    resp = client.messages.create(
        model=GENERATION_MODEL,
        max_tokens=700,
        system=(
            BASE_STYLE + " Answer the enquiry using ONLY the knowledge base "
            "below. If any part of the enquiry cannot be answered from the "
            "knowledge base, do not guess: say that part will be passed to the "
            "information desk, and set answered_from_kb to false.\n\n"
            f"<knowledge_base>\n{kb}\n</knowledge_base>"
        ),
        tools=[KB_REPLY_TOOL],
        tool_choice={"type": "tool", "name": "kb_reply"},
        messages=[{"role": "user", "content": f"Enquiry received:\n\n{text}"}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    return block.input["reply"].strip(), bool(block.input["answered_from_kb"])


def draft_service_confirmation(text: str, c: Classification, client) -> str:
    loc = c.entities.get("location", "the reported location")
    return _generate(
        client,
        BASE_STYLE + " This confirms a facilities/EVS service request has been "
        "logged and routed. Mention the reported location and that the team "
        "will attend within the service window.",
        f"Service request (location: {loc}):\n\n{text}",
    )


def draft_urgent_ack(text: str, c: Classification, client) -> str:
    return _generate(
        client,
        BASE_STYLE + " This is a HOLDING acknowledgement for an urgent safety "
        "escalation. Confirm the report is received and a duty supervisor has "
        "been alerted immediately. Do NOT attempt to resolve or advise -- a "
        "human is taking over. Two or three sentences maximum.",
        f"Urgent report received:\n\n{text}",
    )


# ---------------------------------------------------------------------------
# Mock generators -- same signatures, template output
# ---------------------------------------------------------------------------

def mock_complaint_ack(text: str, c: Classification) -> str:
    return (
        "Thank you for taking the time to share this with us, and we are sorry "
        "your experience fell short of what you should expect from us. Your "
        "complaint has been escalated to our Patient Liaison Office as a "
        "priority, and a member of the team will contact you within 2 hours. "
        "[mock draft]"
    )


_KB_TOPICS = ["visiting", "hours", "parking", "records", "cafeteria", "wifi",
              "wi-fi", "appointment", "feedback", "complaint"]


def mock_kb_reply(text: str, c: Classification) -> tuple[str, bool]:
    """Mirrors the live signature: (reply_text, answered_from_kb)."""
    low = text.lower()
    if any(t in low for t in _KB_TOPICS):
        return (
            "Thanks for getting in touch. Maternity ward visiting hours are "
            "10:00am-1:00pm and 4:00pm-8:00pm daily. Paid parking is available in "
            "the multi-storey car park on Hospital Drive ($4/hour, $18 daily max), "
            "with a free 15-minute pick-up zone at the main entrance. [mock draft]",
            True,
        )
    return (
        "Thanks for getting in touch. This isn't something I can answer from our "
        "standard information, so I've passed your question to our information "
        "desk — they'll come back to you directly. [mock draft]",
        False,
    )


def mock_service_confirmation(text: str, c: Classification) -> str:
    loc = c.entities.get("location", "the reported location")
    return (
        f"Your service request for {loc} has been logged and routed to our "
        "Facilities & Environmental Services team, who will attend within the "
        "standard service window. Reference details are in your case log entry. "
        "[mock draft]"
    )


def mock_urgent_ack(text: str, c: Classification) -> str:
    return (
        "Your urgent report has been received and the on-call duty supervisor "
        "has been alerted immediately. A staff member is being dispatched now. "
        "[mock draft]"
    )
