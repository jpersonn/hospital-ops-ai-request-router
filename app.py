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
from models import ProcessedRequest, Urgency
from samples import SAMPLE_REQUESTS
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

    cols = st.columns(len(SAMPLE_REQUESTS))
    for col, (label, text) in zip(cols, SAMPLE_REQUESTS.items()):
        if col.button(label, use_container_width=True):
            st.session_state.request_text = text

    request_text = st.text_area(
        "Request text",
        value=st.session_state.request_text,
        height=140,
        placeholder="Paste an incoming email, form submission, or inbox message...",
    )

    process = st.button("Process request", type="primary")

    if process and request_text.strip():
        with st.spinner("Classifying..."):
            classification = classify(request_text, client=CLIENT, use_mock=use_mock)

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
        if below:
            st.warning(
                "⚠️ Confidence is below the threshold — this request is diverted to "
                "the **human review queue** instead of being auto-processed. "
                "(Escalation override in action.)"
            )

        st.subheader("3 · Remediation")
        st.info(
            "🚧 Branch-specific remediation workflows are added in Block 2. "
            "Right now the request is classified and logged; next it will trigger "
            "its type-specific multi-step workflow."
        )

        # Persist (no actions yet in Block 1)
        pr = ProcessedRequest(
            request_id=str(uuid.uuid4())[:8],
            raw_text=request_text.strip(),
            classification=classification,
            actions=[],
            final_status="needs_review" if below else "classified",
        )
        storage.save_request(pr)
        st.success(f"Logged as request `{pr.request_id}`.")

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
        m[1].metric("Types seen", len(by_type))
        m[2].metric("Needs review", by_status.get("needs_review", 0))
        m[3].metric("Classified", by_status.get("classified", 0))
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
