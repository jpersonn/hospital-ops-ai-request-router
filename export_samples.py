"""Export one end-to-end run per sample request to sample_outputs/ as JSON.

Mirrors app.py's processing flow exactly (classify -> policy override ->
confidence gate -> branch execution) but writes files instead of rendering,
and never touches the audit database. This produces the "sample input +
corresponding output log" assets the brief requires, reproducibly:

    python export_samples.py          # mock mode -- deterministic, no API key
    python export_samples.py --live   # live Claude calls -- needs .env key

Each JSON file contains the raw input, the full classification, every
remediation action (including generated artifacts), and the final status.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from classifier import classify
from config import CONFIDENCE_THRESHOLD
from models import ActionRecord, ProcessedRequest
from samples import SAMPLE_REQUESTS
from workflow import (
    WorkflowContext,
    apply_policy_overrides,
    check_actionability,
    run_branch,
)

OUT_DIR = Path(__file__).parent / "sample_outputs"


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def process_one(label: str, text: str, client, use_mock: bool) -> ProcessedRequest:
    """Same pipeline app.py runs for a single request."""
    classification = classify(text, client=client, use_mock=use_mock)
    override_note = apply_policy_overrides(classification)

    if classification.out_of_scope:
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
    elif (missing := check_actionability(classification)):
        actions = [
            ActionRecord(
                "Actionability check",
                "flagged",
                f"Classified as {classification.request_type.value} "
                f"({classification.urgency.value}), but required details are "
                f"missing: {', '.join(missing)}. No automated remediation was "
                "run — an operator must obtain the missing information first.",
            )
        ]
        final_status = "needs_info"
    elif classification.confidence < CONFIDENCE_THRESHOLD:
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
            raw_text=text.strip(),
            classification=classification,
            client=client,
            use_mock=use_mock,
        )
        actions, final_status = run_branch(ctx)
        if override_note:
            actions.insert(0, ActionRecord("Policy override", "flagged", override_note))

    return ProcessedRequest(
        request_id=_slug(label),
        raw_text=text.strip(),
        classification=classification,
        actions=actions,
        final_status=final_status,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live", action="store_true",
        help="use live Claude calls instead of deterministic mocks",
    )
    args = parser.parse_args()

    load_dotenv()
    client = None
    if args.live:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("--live requires ANTHROPIC_API_KEY (copy .env.example to .env).")
        from anthropic import Anthropic
        client = Anthropic(api_key=key)
    use_mock = not args.live

    OUT_DIR.mkdir(exist_ok=True)
    mode = "live" if args.live else "mock"
    print(f"Exporting {len(SAMPLE_REQUESTS)} samples in {mode} mode -> {OUT_DIR.name}/\n")

    for label, text in SAMPLE_REQUESTS.items():
        pr = process_one(label, text, client, use_mock)
        record = pr.to_dict()
        record["mode"] = mode
        path = OUT_DIR / f"{pr.request_id}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(
            f"  {label:32s} -> {pr.classification.request_type.value:32s} "
            f"| {pr.classification.urgency.value:8s} | {pr.final_status:12s} "
            f"| {path.name}"
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
