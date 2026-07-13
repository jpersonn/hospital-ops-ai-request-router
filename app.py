"""Hospital Operations Request Router -- Streamlit UI.

End-to-end flow: intake -> AI classification (type + urgency) -> policy
override + confidence gate -> branch-specific remediation -> audit log,
plus a dashboard tab summarising volumes and statuses.
"""

from __future__ import annotations

import os
import uuid

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from classifier import classify
from config import CONFIDENCE_THRESHOLD, CLASSIFIER_MODEL
from models import ActionRecord, ProcessedRequest, Urgency
from samples import SAMPLE_REQUESTS
from workflow import (
    WorkflowContext,
    apply_policy_overrides,
    check_actionability,
    run_branch,
)
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

# Shared by the Process tab's action summary and the Dashboard audit trail.
STATUS_ICON = {"done": "✅", "flagged": "🚩", "paused": "⏸️", "error": "❌"}

# Colour coding for the dashboard table and audit-trail dialog. Urgency
# reuses the Process tab's palette (keyed by name here, since DB rows are
# plain strings); type and final status get their own muted palettes.
URGENCY_COLOUR_BY_NAME = {u.value: c for u, c in URGENCY_COLOUR.items()}
TYPE_COLOUR = {
    "Facility / EVS request": "#0E7490",
    "Patient experience complaint": "#C2410C",
    "General enquiry": "#2563EB",
    "Urgent safety escalation": "#A32D2D",
}
STATUS_COLOUR = {
    "resolved": "#1D9E75",
    "routed": "#2166C4",
    "escalated": "#8B3FBF",
    "human_review": "#A32D2D",
    "needs_review": "#BA7517",
    "needs_info": "#6B7280",
}


def _chip_html(text: str, colour: str) -> str:
    return (
        f"<span style='background:{colour};color:white;padding:2px 10px;"
        f"border-radius:12px;font-weight:600;font-size:0.85em'>{text}</span>"
    )


@st.dialog("Request audit trail", width="large")
def show_audit_trail(r: dict):
    st.markdown(
        f"`{r['request_id']}` &nbsp; "
        + _chip_html(r["request_type"], TYPE_COLOUR.get(r["request_type"], "#555"))
        + " "
        + _chip_html(r["urgency"], URGENCY_COLOUR_BY_NAME.get(r["urgency"], "#555"))
        + " "
        + _chip_html(r["final_status"], STATUS_COLOUR.get(r["final_status"], "#555")),
        unsafe_allow_html=True,
    )
    st.caption(
        f"Confidence {r['confidence']:.0%} · "
        f"received {r['created_at'][:19].replace('T', ' ')} UTC"
    )
    if r["reasoning"]:
        st.write(f"**Classifier reasoning:** {r['reasoning']}")
    st.write("**Raw request:**")
    st.text(r["raw_text"])
    st.write("**Actions taken:**")
    for i, a in enumerate(storage.get_actions_for(r["request_id"]), start=1):
        icon = STATUS_ICON.get(a["status"], "•")
        st.markdown(f"{icon} **Step {i} — {a['step_name']}**  \n{a['detail']}")
        if a["artifact"]:
            with st.expander(f"View generated output — {a['step_name']}"):
                st.text(a["artifact"])

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
    # Input on the left, outcome on the right: the whole story -- request,
    # classification, remediation -- stays on one screen with no scrolling.
    col_in, col_out = st.columns([5, 4], gap="large")

    with col_in:
        st.subheader("1 · Incoming request")

        if "request_text" not in st.session_state:
            st.session_state.request_text = ""

        labels = list(SAMPLE_REQUESTS.items())
        st.caption(
            "Pre-loaded samples exercise each branch and the safety mechanisms "
            "— click to load, or paste your own below."
        )
        for row_start in range(0, len(labels), 2):
            cols = st.columns(2)
            for col, (label, text) in zip(cols, labels[row_start:row_start + 2]):
                if col.button(label, width="stretch"):
                    st.session_state.request_text = text

        request_text = st.text_area(
            "Request text",
            value=st.session_state.request_text,
            height=240,
            placeholder="Paste an incoming email, form submission, or inbox message...",
        )

        process = st.button("Process request", type="primary")

    with col_out:
        if process and len(request_text.strip()) < 20:
            st.error(
                "Request is too short to classify meaningfully (under 20 characters). "
                "Please provide the full request text."
            )
        elif process and request_text.strip():
            with st.spinner("Classifying..."):
                try:
                    classification = classify(request_text, client=CLIENT, use_mock=use_mock)
                except Exception as exc:  # noqa: BLE001 -- API/network failures must not stack-trace
                    st.error(
                        f"Classification call failed ({type(exc).__name__}). Nothing was "
                        "processed or logged — try again, or switch to mock mode in the sidebar."
                    )
                    st.stop()

            override_note = apply_policy_overrides(classification)

            st.subheader("2 · Classification")
            st.markdown(
                _chip_html(
                    classification.request_type.value,
                    TYPE_COLOUR.get(classification.request_type.value, "#555"),
                )
                + " "
                + _chip_html(
                    classification.urgency.value,
                    URGENCY_COLOUR[classification.urgency],
                )
                + f" &nbsp; **{classification.confidence:.0%}** confidence",
                unsafe_allow_html=True,
            )
            st.progress(min(classification.confidence, 1.0))
            st.write(f"**Sub-topic:** {classification.sub_topic}")
            st.write(f"**Reasoning:** {classification.reasoning}")
            if classification.entities:
                st.write("**Extracted details:**", classification.entities)

            below = classification.confidence < CONFIDENCE_THRESHOLD
            missing = check_actionability(classification)

            st.subheader("3 · Remediation")

            if override_note:
                st.info(f"🛡️ {override_note}")

            if classification.out_of_scope:
                st.warning(
                    "🛑 The classifier flagged this as **not a genuine operations "
                    "request** (possible prompt injection or spam). It is quarantined "
                    "for human review — no automated reply was generated or sent."
                )
                actions = [
                    ActionRecord(
                        "Quarantine for human review",
                        "flagged",
                        "Classifier flagged this message as out of scope (possible "
                        "prompt injection or spam). No automated remediation was run "
                        "and no reply was sent.",
                    )
                ]
                final_status = "needs_review"
            elif missing:
                miss_str = ", ".join(missing)
                st.warning(
                    f"⚠️ Classified as {classification.request_type.value}, but the "
                    f"request is missing: **{miss_str}**. Diverted to human review — "
                    "an operator needs to obtain this information before automation "
                    "can proceed."
                )
                actions = [
                    ActionRecord(
                        "Actionability check",
                        "flagged",
                        f"Classified as {classification.request_type.value} "
                        f"({classification.urgency.value}), but required details are "
                        f"missing: {miss_str}. No automated remediation was run — an "
                        "operator must obtain the missing information first.",
                    )
                ]
                final_status = "needs_info"
            elif below:
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
        else:
            st.caption(
                "Classification and remediation results will appear here once "
                "you process a request."
            )

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
            by_status.get("human_review", 0)
            + by_status.get("needs_review", 0)
            + by_status.get("needs_info", 0),
        )
        m[3].metric(
            "Routed / escalated",
            by_status.get("routed", 0) + by_status.get("escalated", 0),
        )
        st.write("**Volumes by type:**")
        st.bar_chart(by_type)
        st.write("**Request log — select a row to open its full audit trail:**")
        df = pd.DataFrame(
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
            ]
        )

        def _cell_style(colour_map):
            return lambda v: (
                f"background-color:{colour_map[v]};color:white;font-weight:600"
                if v in colour_map else ""
            )

        styled = (
            df.style
            .map(_cell_style(TYPE_COLOUR), subset=["type"])
            .map(_cell_style(URGENCY_COLOUR_BY_NAME), subset=["urgency"])
            .map(_cell_style(STATUS_COLOUR), subset=["status"])
        )
        # The table's key includes a nonce that is bumped every time a dialog
        # opens: the next rerun then mounts a fresh, unselected grid. Without
        # this, the selection would persist after the dialog closes and
        # clicking the same row again would not re-fire a selection event.
        if "audit_table_nonce" not in st.session_state:
            st.session_state.audit_table_nonce = 0
        event = st.dataframe(
            styled,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=f"audit_table_{st.session_state.audit_table_nonce}",
        )

        selected = event.selection.rows
        if selected:
            st.session_state.audit_table_nonce += 1
            show_audit_trail(rows[selected[0]])
