# Hospital Operations Request Router

An AI-assisted triage and remediation prototype for non-clinical hospital
operations requests — the kind of things a shared inbox at a large hospital
receives every day: a facilities job, a patient experience complaint, a general
enquiry, a safety escalation. Each incoming message is classified, checked
against a series of policy gates, and — if eligible — put through a
type-specific multi-step remediation workflow whose every action is captured in
an audit trail.

Built as a 5-day proof of concept for the Firstsource Solutions **Consultant —
AI & Analytics** recruitment process, in response to their *Incoming Request
Processing Workflow* brief. The domain choice (hospital operations) reflects
my day job as an environmental services assistant at a hospital.

> **Non-clinical scope.** This system does not provide medical advice and is
> not a clinical triage tool. Messages that describe a possible risk to a
> person are flagged for immediate human handling, never auto-resolved.

---

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at `http://localhost:8501`. It runs immediately in **mock mode**
with no API key needed — a keyword-based classifier and template drafts stand
in for the live model so the whole pipeline is exercisable offline.

For **live mode** with Claude:

```bash
cp .env.example .env         # then paste your key into .env
```

Restart the app, and turn off *Mock mode* in the sidebar. Classification runs
on Claude Haiku 4.5; drafted patient-facing replies run on Claude Sonnet 4.6.

---

## What it does

Six pre-loaded samples exercise every branch and every safety mechanism:

| Sample | What it demonstrates |
|---|---|
| Facility / EVS request | Full happy-path facility branch: entity extraction, work-order generation, requester confirmation, SLA timer |
| Patient experience complaint | Adaptive complaint acknowledgement (empathetic, no false promises), escalation notice, priority logging |
| General enquiry | KB-grounded auto-reply, marked resolved when the KB covered the question |
| Enquiry (not in KB) | Same branch, different path: reply promises a handoff, case is routed rather than resolved |
| Urgent safety escalation | Immediate human handoff, holding acknowledgement, supervisor alert, auto-resolution paused |
| Ambiguous (edge case) | Deliberately vague; classifier reports low confidence and the case is diverted before any automation runs |
| Adversarial (injection) | A prompt-injection attempt; quarantined by the legitimacy gate before it reaches any branch |

Live audit trails from a real end-to-end run of every sample are committed
under [`sample_outputs/`](sample_outputs/).

---

## Architecture

Three layers, each in its own module set:

```
Data          models.py, config.py, samples.py, knowledge_base.md
Logic         classifier.py, workflow.py, generation.py, storage.py
Interface     app.py                     (Streamlit)
                                         export_samples.py (batch runner)
```

The pipeline for a single request:

```
raw text
   │
   ▼
[ Classifier ]  ── Claude Haiku, forced tool-use, structured output
   │
   ▼
[ Legitimacy gate ]     ─── quarantine if out_of_scope
[ Policy override ]     ─── Critical urgency → safety branch
[ Actionability gate ]  ─── missing required entities → info-request reply
[ Confidence gate ]     ─── < 0.60 → human review
   │
   ▼
[ Branch executor ]   ── declarative Step list per RequestType
   │                      each Step returns (status, detail, artifact)
   ▼
[ SQLite audit log ]  ── one row per action, dashboard-readable
```

**Classification and remediation are cleanly separated.** The classifier
returns a schema-validated `Classification` object and nothing else. Every
downstream decision — which branch runs, whether it runs at all, which
artifacts get generated — is made by explicit code the reviewer can point at.
The model classifies; policy decides.

---

## The seven safety mechanisms

Every uncertainty path in this system fails **toward a human**. That
principle drove the design of each gate below.

### 1. Legitimacy gate — out-of-scope quarantine
The classifier returns `out_of_scope: bool` as a first-class schema field
(distinct from confidence). Messages the model identifies as prompt injection,
spam, or system manipulation are quarantined *before* any branch executes. No
draft reply is generated; no team is notified. **This gate was added in
response to a specific failure:** the injection sample was classified at 0.95
confidence, because confidence measures certainty of the *label*, not
legitimacy of the *message*. Legitimacy is its own dimension.

### 2. Policy override — Critical urgency
If the classifier assigns `Urgency.CRITICAL` to any type, policy overrides the
branch to `URGENT_SAFETY` regardless of the type label. A "biohazard spill in
the corridor" phrased as a facilities job must not receive routine routing
because the type label was benign. The original type label is preserved in
the audit trail.

### 3. Actionability gate — required entities per branch
Some branches cannot execute without specific extracted entities. A facilities
job with no location cannot be dispatched. This gate checks required entities
per branch:

- `Facility / EVS request` → requires `location`
- `Patient experience complaint` → no requirement (see design note below)
- `General enquiry` → no requirement
- `Urgent safety escalation` → no requirement (Critical reports must never be
  held at an info gate)

When the gate fires, the branch does not run; a reply is drafted asking the
requester to supply the missing details, and the case is held open in the
`needs_info` queue.

**Design note.** An earlier version of this gate treated complaints
identically — no callback contact meant divert. This produced a worse outcome
than running the branch: complaints without a contact are still worth
escalating, logging, and acknowledging. The acknowledgement text simply
adapts — with a contact it promises a callback; without one it invites the
requester to reply if they want personal follow-up. **Only gate what makes
the branch's core action impossible.**

### 4. Confidence threshold
Below `CONFIDENCE_THRESHOLD` (0.60), the classification is treated as
insufficiently certain to trust automation with. The case is diverted to
human review without running any branch.

### 5. Trivial input guard
Requests under 20 characters are rejected before the classifier is called.
Cheap, and it stops accidental submissions (a blank textarea, a keystroke, a
paste of a single word) from spending tokens and creating noise in the audit log.

### 6. Step exception → divert
If any step handler raises an exception, `run_branch` records the failure as
an audit row and diverts the case to human review with a second audit row
naming the divert. A partial audit trail plus a human handoff beats a stack
trace, always.

### 7. KB unanswerable → route
The KB-grounded reply generator returns a boolean `answered_from_kb`
alongside the drafted text. If any part of the enquiry can't be answered from
the knowledge base, the model sets this false and the branch's
`resolve_or_route` step marks the case `routed` (to the info desk) rather
than `resolved`. **The case status always matches what the drafted reply
promised the enquirer.**

---

## The four branches

Each branch is defined declaratively in `workflow.py` as an ordered list of
`Step` objects. Adding a fifth branch is a dict entry, not new control flow.

### Facility / EVS request
1. Extract details from the classification's entity fields
2. Route to Facilities & Environmental Services — generates a plain-text work
   order (type, urgency, sub-topic, location, requester+contact, SLA, summary)
3. Draft a confirmation for the requester
4. Set SLA timer

### Patient experience complaint
1. Draft an empathetic acknowledgement (adaptive — see actionability gate above)
2. Escalate to Patient Liaison Office — generates an escalation notice
3. Log with priority flag
4. Set follow-up reminder

### General enquiry
1. Extract sub-topic and entities
2. Draft KB-grounded reply (structured tool call returning
   `(reply, answered_from_kb)`)
3. Resolve or route — the branch's terminal state depends on step 2's boolean
4. Log case

### Urgent safety escalation
1. Flag for human review immediately
2. Draft a holding acknowledgement — no attempt to resolve or advise
3. Notify duty supervisor with a supervisor alert
4. Pause auto-resolution

---

## Key design decisions

### Forced tool-calling for classification
The classifier uses Claude's tool-use with `tool_choice={"type":"tool", ...}`
to force a schema-valid response. The model must call `classify_request` with
enum-constrained fields (`request_type`, `urgency`) and a fixed shape. The
response cannot be free text; there is no parsing step; the model cannot
invent a fifth category. Structured output is the difference between
"classification works most of the time" and "the pipeline downstream of
classification is defensible."

The same pattern is used for the KB reply (`draft_kb_reply` returns
`(reply, answered_from_kb)` via forced tool use) so that the workflow can
route on the model's own judgement rather than parsing the drafted text for
phrases like "I'll pass this along."

### Two models, split by job
Classification runs on `claude-haiku-4-5-20251001` (fast, cheap, well-suited
to structured extraction). Patient-facing drafted text runs on
`claude-sonnet-4-6` (better prose quality for empathetic acknowledgements and
KB-grounded replies). Deterministic artifacts — the work order, the
escalation notice, the supervisor alert — are templated Python because they
are structured operational data and don't need a model at all.

### Branches as data, not code
`workflow.BRANCHES` is a dict mapping `RequestType` to a list of `Step`
objects. `run_branch` is one generic loop that iterates the list, calls each
handler with a shared `WorkflowContext`, and emits an `ActionRecord` per
step. This means the remediation strategy is auditable at a glance (the dict
*is* the documentation) and extensible without touching the executor. A
handler can pass data forward to later steps via `ctx.shared`; this is how
the enquiry branch's `resolve_or_route` reads the KB reply's boolean.

### ActionRecord is dual-purpose by design
The same dataclass is what the UI renders in the action summary *and* what
the SQLite audit table stores as a row. No translation layer. The
brief's "outputs should be legible for an operations team" and its audit
trail requirement are the same thing at different display surfaces.

### Mock mode has full signature parity with live mode
Every live generator function has a `mock_*` counterpart with matching
signatures. Mock mode is (a) a development affordance — the whole pipeline
is testable without spending tokens — and (b) demo insurance — a hosted
demo can't fail from a network hiccup or API outage. The mock classifier
uses a conservative regex extractor for entities (locations like "ward 3B",
phone numbers, emails) so that entity-gated branches remain exercisable
offline.

---

## Sample outputs

`sample_outputs/` contains one JSON per sample from a live-mode run — the
full audit trail as persisted, including every action, artifact, and
timestamp. This is the same shape the SQLite audit log stores. To regenerate,
run `python export_samples.py` with a valid `ANTHROPIC_API_KEY`.

---

## Production considerations (not built in this POC)

The rubric explicitly rewards judgement on what to leave out. These are the
things a production version would need, deliberately scoped out of a 5-day
POC:

- **Real RAG for the knowledge base.** The current implementation injects
  `knowledge_base.md` whole into the prompt. At POC scale (2 KB, ~250 tokens)
  this is honest and simple. Production would chunk the KB, embed the
  chunks, retrieve top-k by cosine similarity, and cite the retrieved chunk
  IDs in the audit trail for defensibility. The interface (`draft_kb_reply`
  taking the enquiry text and returning `(reply, answered_from_kb)`) would
  not change — only the internals.
- **Multi-turn state.** When the actionability gate fires and asks the
  requester for more information, the current system holds the case open but
  has no mechanism to correlate a follow-up reply back to the original case.
  Production would need thread-ID/case-ID correlation, a timeout policy for
  unanswered info requests, and re-classification of the combined
  original+follow-up as a single message.
- **PII handling.** The audit log currently stores raw request text. A
  production system needs entity redaction (patient names, medical record
  numbers, phone numbers) before persistence, plus a retention policy per
  case status.
- **Multi-intent requests.** A single message that is both a complaint and a
  service request is currently forced into the dominant intent. Production
  would split into linked cases and run both branches with the correlation
  visible in the audit trail.
- **Human review UI.** Cases in `needs_review`, `needs_info`, and
  `human_review` are all visible in the dashboard, but there is no UI for
  an operator to act on them — take ownership, add notes, transition status.
  This is the biggest gap between the POC and a deployable tool.
- **MLOps for the classifier.** No evaluation harness, no drift monitoring,
  no versioned prompts, no A/B evaluation of classifier prompt changes. A
  labelled test set with per-branch precision/recall metrics is the first
  thing to add.

---

## Repository layout

```
app.py                 Streamlit UI — process tab, dashboard tab, audit modal
classifier.py          Claude classification (forced tool use) + keyword mock
workflow.py            Branch registry, step handlers, gates, executor
generation.py          Sonnet-drafted messages + template mocks
storage.py             SQLite audit log with per-request migrations
models.py              Enums and dataclasses shared across layers
config.py              Model names, threshold, routing table, SLAs
samples.py             Seven pre-loaded samples exercising every mechanism
knowledge_base.md      Small operations KB grounding enquiry replies
export_samples.py      Batch runner that regenerates sample_outputs/
sample_outputs/        Live-mode JSON dumps, one per sample
```

---

## License

MIT — see `LICENSE`.

---

*Jason Huang · Final-year Software Engineering (Honours), Monash University.
Built July 2026 for the Firstsource AI & Analytics recruitment process.*
