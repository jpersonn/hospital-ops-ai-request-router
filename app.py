"""Hospital Operations Request Router -- Streamlit UI.

Block 1 scope: intake -> AI classification -> human-review flag -> audit log.
The remediation engine (branch-specific workflows) is added in Block 2; its
section below is a clearly-marked stub so the app runs end-to-end today.
"""

from __future__ import annotations

import os
import uuid

import streamlit as st
from dotenv import load_dotenv

from classifier import classify
from config import CONFIDENCE_THRESHOLD, CLASSIFIER_MODEL
from models import ActionRecord, ProcessedRequest, Urgency
from samples import SAMPLE_REQUESTS
from workflow import WorkflowContext, apply_policy_overrides, run_branch
import storage

load_dotenv()
storage.init_db()

st.set_page_config(page_title="Hospital Ops Request Router", page_icon="🏥", layout="wide")

# --- Client setup -----------------------------------------------------------
def get_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=key)
    except Exception:
        return None

CLIENT = get_client()

URGENCY_COLOUR = {
    Urgency.LOW: "#1D9E75",
    Urgency.MEDIUM: "#BA7517",
    Urgency.HIGH: "#D85A30",
    Urgency.CRITICAL: "#A32D2D",
}

# --- Sidebar ----------------------------------------------------------------
with st.sidebar:
    st.header("Controls")
    default_mock = CLIENT is None
    use_mock = st.toggle(
        "Mock mode (no API calls)",
        value=default_mock,
        help="On = deterministic keyword classifier. Off = live Claude classification.",
    )
    if CLIENT is None and not use_mock:
        st.warning("No ANTHROPIC_API_KEY found — mock mode is required until you add one.")
        use_mock = True
    st.caption(f"Classifier model: `{CLASSIFIER_MODEL}`")
    st.caption(f"Confidence threshold: **{CONFIDENCE_THRESHOLD:.2f}**")
    st.divider()
    if st.button("Reset audit log", use_container_width=True):
        storage.reset_db()
        st.success("Audit log cleared.")

# --- Header -----------------------------------------------------------------
st.title("🏥 Hospital Operations Request Router")
st.caption(
    "AI-assisted triage and remediation for **non-clinical** operations requests. "
    "This tool does not provide medical advice; genuine clinical emergencies are "
    "flagged for immediate human handling, not auto-resolved."
)

tab_process, tab_dashboard = st.tabs(["Process a request", "Dashboard"])

# ===========================================================================
# PROCESS TAB
# ===========================================================================
with tab_process:
    st.subheader("1 · Incoming request")

    if "request_text" not in st.session_state:
        st.session_state.request_text = ""

    labels = list(SAMPLE_REQUESTS.items())
    for row_start in range(0, len(labels), 4):
        cols = st.columns(4)
        for col, (label, text) in zip(cols, labels[row_start:row_start + 4]):
            if col.button(label, use_container_width=True):
                st.session_state.request_text = text

    request_text = st.text_area(
        "Request text",
        value=st.session_state.request_text,
        height=140,
        placeholder="Paste an incoming email, form submission, or inbox message...",
    )

    process = st.button("Process request", type="primary")

    if process and len(request_text.strip()) < 20:
        st.error(
            "Request is too short to classify meaningfully (under 20 characters). "
            "Please provide the full request text."
        )
    elif process and request_text.strip():
        with st.spinner("Classifying..."):
            classification = classify(request_text, client=CLIENT, use_mock=use_mock)

        override_note = apply_policy_overrides(classification)

        st.subheader("2 · Classification")
        c1, c2, c3 = st.columns(3)
        c1.metric("Type", classification.request_type.value)
        colour = URGENCY_COLOUR[classification.urgency]
        c2.markdown(
            f"**Urgency**<br><span style='background:{colour};color:white;"
            f"padding:3px 12px;border-radius:12px;font-weight:600'>"
            f"{classification.urgency.value}</span>",
            unsafe_allow_html=True,
        )
        c3.metric("Confidence", f"{classification.confidence:.0%}")

        st.progress(min(classification.confidence, 1.0))
        st.write(f"**Sub-topic:** {classification.sub_topic}")
        st.write(f"**Reasoning:** {classification.reasoning}")
        if classification.entities:
            st.write("**Extracted details:**", classification.entities)

        below = classification.confidence < CONFIDENCE_THRESHOLD

        st.subheader("3 · Remediation")

        if override_note:
            st.info(f"🛡️ {override_note}")

        if below:
            st.warning(
                "⚠️ Confidence is below the threshold — this request is diverted to "
                "the **human review queue** instead of being auto-processed. "
                "(Escalation override in action.)"
            )
            actions = [
                ActionRecord(
                    "Divert to human review queue",
                    "flagged",
                    f"Classification confidence {classification.confidence:.0%} is "
                    f"below the {CONFIDENCE_THRESHOLD:.0%} threshold. No automated "
                    "remediation was run.",
                )
            ]
            final_status = "needs_review"
        else:
            ctx = WorkflowContext(
                raw_text=request_text.strip(),
                classification=classification,
                client=CLIENT,
                use_mock=use_mock,
            )
            with st.spinner("Running remediation branch..."):
                actions, final_status = run_branch(ctx)
            if override_note:
                actions.insert(0, ActionRecord(
                    "Policy override", "flagged", override_note,
                ))

        # --- Action summary: one line per step, artifacts in expanders ---
        STATUS_ICON = {"done": "✅", "flagged": "🚩", "paused": "⏸️", "error": "❌"}
        for i, a in enumerate(actions, start=1):
            icon = STATUS_ICON.get(a.status, "•")
            st.markdown(f"{icon} **Step {i} — {a.step_name}**  \n{a.detail}")
            if a.artifact:
                with st.expander(f"View generated output — {a.step_name}"):
                    st.text(a.artifact)

        st.markdown(f"**Final status:** `{final_status}`")

        # Persist the full record: classification + every action
        pr = ProcessedRequest(
            request_id=str(uuid.uuid4())[:8],
            raw_text=request_text.strip(),
            classification=classification,
            actions=actions,
            final_status=final_status,
        )
        storage.save_request(pr)
        st.success(f"Logged as request `{pr.request_id}` with {len(actions)} audit entries.")

    elif process:
        st.error("Please enter or select a request first.")

# ===========================================================================
# DASHBOARD TAB
# ===========================================================================
with tab_dashboard:
    st.subheader("Processed requests")
    rows = storage.all_requests()
    if not rows:
        st.caption("No requests logged yet — process one to populate the dashboard.")
    else:
        by_type = storage.summary_by_type()
        by_status = storage.summary_by_status()
        m = st.columns(4)
        m[0].metric("Total", len(rows))
        m[1].metric("Resolved", by_status.get("resolved", 0))
        m[2].metric(
            "Human review",
            by_status.get("human_review", 0) + by_status.get("needs_review", 0),
        )
        m[3].metric(
            "Routed / escalated",
            by_status.get("routed", 0) + by_status.get("escalated", 0),
        )
        st.write("**Volumes by type:**")
        st.bar_chart(by_type)
        st.dataframe(
            [
                {
                    "id": r["request_id"],
                    "type": r["request_type"],
                    "urgency": r["urgency"],
                    "confidence": f'{r["confidence"]:.0%}',
                    "status": r["final_status"],
                    "created": r["created_at"][:19].replace("T", " "),
                }
                for r in rows
            ],
            use_container_width=True,
            hide_index=True,
        )
