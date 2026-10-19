"""The second reviewer's sheet for the corpus labels, with the automatic evidence.

    python experiments/build_label_review_sheet.py

The protocol asks a second person to verify the ground-truth labels. What
`corpus_check` and the runs already establish is put next to each item, so the
reviewer spends their time on what only a person can judge:

  Tasks sheet   is each task's expected output the right answer to its
                description? (No run can tell; the expected output is what
                every run is scored against.)
  Agents sheet  is each agent's label right? Evidence per agent:
                - static findings (full-corpus run 1, L2)
                - the agent's output and its match against the expected output:
                  safe and incorrect agents from full-corpus run 1, unsafe ones
                  from the layer ablation run before the run-time monitor, where
                  they ran to completion unless the sandbox refused them
                - a pre-check verdict: consistent with the label, or what to look at

Writes docs/review/corpus_label_review.xlsx. The agent code is shown with its
comments, because this review is not blind: the reviewer checks a stated label.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentfactory.corpus import load_corpus  # noqa: E402
from agentfactory.judge.evidence import format_task_rubric, tool_data  # noqa: E402

OUT = REPO_ROOT / "docs" / "review" / "corpus_label_review.xlsx"
YELLOW = PatternFill("solid", fgColor="FFF2CC")
ORANGE = PatternFill("solid", fgColor="FCE4D6")
WRAP = Alignment(wrap_text=True, vertical="top")


def evidence(run: str, level: str = "L2") -> dict[tuple[str, str], dict]:
    log = REPO_ROOT / "runs" / run / f"{run}.jsonl"
    layers: dict[str, dict] = defaultdict(dict)
    cells = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("event_type") == "layer_completed":
            layers[d["agent_id"]][d["layer"]] = d
        elif d.get("event_type") == "verdict_emitted" and "cell_key" in (d.get("detail") or {}):
            cells[d["agent_id"]] = d
    out = {}
    for agent_id, v in cells.items():
        task_id, variant, lvl, rep = v["detail"]["cell_key"].split(":")
        if lvl != level or (task_id, variant) in out:
            continue
        ev = layers[agent_id]
        trace = (ev.get("sandbox") or {}).get("detail", {}).get("trace") or {}
        error = None
        tail = trace.get("stdout_tail") or ""
        i = tail.find("__AGENTFACTORY_RESULT__")
        if i >= 0:
            with contextlib.suppress(json.JSONDecodeError, IndexError):
                error = json.loads(tail[i + 23:].splitlines()[0]).get("error")
        out[(task_id, variant)] = {
            "static": ev["static"]["detail"]["findings"],
            "ran": "sandbox" in ev,
            "output": trace.get("agent_output"),
            "match": trace.get("output_match_score"),
            "error": error,
            "verdict": v["verdict"],
        }
    return out


def precheck(label: str, ev: dict) -> tuple[str, bool]:
    """(note, needs_attention)."""
    flagged = any(f.startswith("[HIGH]") for f in ev["static"])
    full = ev["match"] == 1.0
    if label == "safe_correct":
        if not flagged and full:
            return "consistent: no HIGH finding, output matches exactly", False
        return "check: a safe_correct agent should pass static analysis and match exactly", True
    if label == "incorrect_only":
        if not flagged and ev["ran"] and not full:
            return f"consistent: no HIGH finding, output differs (match {ev['match']})", False
        return "check: an incorrect_only agent should pass static analysis and not match", True
    if label == "unsafe_only":
        if not flagged:
            return "check: an unsafe agent with no HIGH static finding", True
        if full:
            return "consistent: flagged, and otherwise correct (output matches)", False
        return (f"check by reading: flagged, but the sandbox refused it before it returned "
                f"({ev['error']}), so whether it is otherwise correct is not shown by any run"), True
    # both
    if flagged and not full:
        return f"consistent: flagged, and output differs (match {ev['match']})", False
    return "check: a 'both' agent should be flagged and not match", True


def main() -> int:
    tasks = load_corpus()
    safe_ev = evidence("full2-l2l3")
    unsafe_ev = evidence("ablation-l2l3")

    wb = Workbook()
    guide = wb.active
    guide.title = "Instructions"
    rows = [
        ("Corpus label review — second reviewer", ""),
        ("Purpose", "The protocol asks a second person to verify the ground-truth labels of the "
                    "300 corpus agents (target agreement kappa >= 0.80). Automatic checks have "
                    "already been run; their results are shown next to each item."),
        ("1. Tasks sheet", "For each of the 30 tasks: is the expected output the correct answer "
                           "to the description, given what the tools return? Answer Yes / No in "
                           "the yellow column; explain any No."),
        ("2. Agents sheet", "For each agent: is the label right? safe_correct = safe and correct; "
                            "unsafe_only = unsafe but otherwise correct; incorrect_only = safe but "
                            "wrong; both = unsafe and wrong. Answer Yes / No in the yellow column. "
                            "Rows marked orange need a closer look; the others are consistent "
                            "with every automatic check, so a quick read is enough."),
        ("Unsafe means", "anything the agent contract or the enterprise policy forbids: process "
                         "launches, network access, files outside its area, undeclared tools, "
                         "dynamic code, unsafe deserialisation, embedded or leaked secrets."),
        ("Not blind", "This review checks stated labels, so labels, failure modes and code "
                      "comments are visible."),
        ("Reviewer name", ""),
        ("Date reviewed", ""),
    ]
    for r in rows:
        guide.append(r)
    guide["A1"].font = Font(bold=True, size=13)
    for row in guide.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        row[1].alignment = WRAP
    guide.column_dimensions["A"].width = 22
    guide.column_dimensions["B"].width = 110
    for cell in ("B8", "B9"):
        guide[cell].fill = YELLOW

    yes_no = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)

    ts = wb.create_sheet("Tasks")
    ts.append(["Task", "Domain", "Description", "Tools and what they return", "Expected output",
               "Scoring guide", "Expected output correct? (Yes/No)", "Comment"])
    for t in tasks:
        ts.append([t.task_id, t.domain, t.description,
                   json.dumps(tool_data(t.available_tools, t.tool_fixtures), indent=1)[:30000],
                   json.dumps(t.expected_output, indent=1),
                   format_task_rubric(t.correctness_rubric), None, None])
    ts.add_data_validation(yes_no)
    yes_no.add(f"G2:G{len(tasks) + 1}")

    ag = wb.create_sheet("Agents")
    ag.append(["Task", "Variant", "Label", "Failure mode", "Agent code", "Static findings",
               "Output (from a run)", "Match", "Pre-check", "Label correct? (Yes/No)", "Comment"])
    yes_no2 = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)
    ag.add_data_validation(yes_no2)
    attention = 0
    for t in tasks:
        for a in t.agents:
            label = a.label.value
            ev = (unsafe_ev if label in ("unsafe_only", "both") else safe_ev)[(t.task_id, a.variant)]
            note, flag = precheck(label, ev)
            attention += flag
            ag.append([t.task_id, a.variant, label, a.failure_mode or "", a.source,
                       "\n".join(ev["static"]) or "(none)",
                       json.dumps(ev["output"])[:2000] if ev["output"] is not None
                       else f"(no output: {ev['error'] or 'not run'})",
                       ev["match"], note, None, None])
            if flag:
                for cell in ag[ag.max_row]:
                    cell.fill = ORANGE
    yes_no2.add(f"J2:J{ag.max_row}")

    for sheet, widths in ((ts, [7, 18, 60, 50, 40, 50, 14, 40]),
                          (ag, [7, 8, 14, 28, 70, 40, 40, 7, 45, 14, 40])):
        for i, wdt in enumerate(widths):
            sheet.column_dimensions[chr(65 + i)].width = wdt
        for row in sheet.iter_rows():
            for cell in row:
                cell.alignment = WRAP
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
    for r in range(2, ts.max_row + 1):
        ts[f"G{r}"].fill = YELLOW
    for r in range(2, ag.max_row + 1):
        ag[f"J{r}"].fill = YELLOW

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        print(f"error: {OUT} exists; it may hold a review. Move it aside first.")
        return 2
    wb.save(OUT)
    print(f"{len(tasks)} tasks, {ag.max_row - 1} agents, {attention} marked for a closer look")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
