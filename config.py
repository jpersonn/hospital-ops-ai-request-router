"""Tunable constants for the pipeline, kept out of the logic files."""

# --- Models (verified against Anthropic's model list) ---
# Haiku is fast + cheap: ideal for a high-volume classification step.
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
# Sonnet writes better patient-facing prose: used for drafted replies.
GENERATION_MODEL = "claude-sonnet-4-6"

# Below this self-reported confidence, we do NOT trust the branch to run
# automatically -- the request is diverted to the human review queue.
# This is the "escalation override" the brief lists as an optional enhancement.
CONFIDENCE_THRESHOLD = 0.60

# Which team each request type routes to.
ROUTING_TABLE = {
    "Facility / EVS request": "Facilities & Environmental Services",
    "Patient experience complaint": "Patient Liaison Office",
    "General enquiry": "Front-of-House / Info Desk",
    "Urgent safety escalation": "Duty Supervisor (on-call)",
}

# SLA target per urgency level, in hours. Drives the follow-up flags.
SLA_HOURS = {
    "Critical": 0.25,   # 15 minutes -- but really this is human-handled
    "High": 2,
    "Medium": 8,
    "Low": 24,
}
