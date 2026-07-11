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


def draft_kb_reply(text: str, c: Classification, client) -> str:
    kb = KB_PATH.read_text()
    return _generate(
        client,
        BASE_STYLE + " Answer the enquiry using ONLY the knowledge base below. "
        "If the answer is not in the knowledge base, say you will pass the "
        "question to the information desk rather than guessing.\n\n"
        f"<knowledge_base>\n{kb}\n</knowledge_base>",
        f"Enquiry received:\n\n{text}",
    )


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


def mock_kb_reply(text: str, c: Classification) -> str:
    return (
        "Thanks for getting in touch. Maternity ward visiting hours are "
        "10:00am-1:00pm and 4:00pm-8:00pm daily. Paid parking is available in "
        "the multi-storey car park on Hospital Drive ($4/hour, $18 daily max), "
        "with a free 15-minute pick-up zone at the main entrance. [mock draft]"
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
