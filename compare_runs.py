"""Compare a judge.py run with the old judge's results carried in the input.

    python3 compare_runs.py data/pilot.jsonl data/pilot_out.jsonl [--diff data/pilot_diff.csv]

The input JSONL comes from convert_answers.py (ref_* fields hold the old
judge's bank_decision, risk_labels and errors). Prints a JSON summary:
decision cross-table old -> new, per-bank conclusions, what became of each
old error type, error codes of the new run, and risk-label agreement. --diff
writes every case whose decision or labels differ to a CSV for human review
(it contains the question and answer, so keep it under data/).
"""
import argparse
import csv
import json
from collections import Counter, defaultdict


def old_labels(case):
    return sorted(x for x in case.get("ref_risk_labels", "").split("|") if x.strip())


def old_error_type(case):
    return case.get("ref_errors", "").split("|")[0] or None


def compare(cases, recs):
    by_id = {c["case_id"]: c for c in cases}
    judged_ids = {r.get("case_id") for r in recs}
    rows, missing = [], [cid for cid in by_id if cid not in judged_ids]
    cross, per_bank, old_errors = Counter(), defaultdict(Counter), defaultdict(Counter)
    new_errors, label_old, label_new = Counter(), Counter(), Counter()
    label_pairs = same_labels = 0
    for r in recs:
        c = by_id.get(r.get("case_id"))
        if c is None:
            continue
        old, new = c.get("ref_bank_decision"), r["decision"]
        cross[f"{old}->{new}"] += 1
        per_bank[c["bank"]][f"content={r.get('content_result')}|refusal={r.get('refusal_result')}"] += 1
        if old_error_type(c):
            old_errors[old_error_type(c)][new if new != "ERROR" else f"ERROR:{r.get('error_code')}"] += 1
        if new == "ERROR":
            new_errors[r.get("error_code")] += 1
        ol, nl = old_labels(c), sorted(r.get("risk_labels") or [])
        if old != "ERROR" and new != "ERROR":
            label_pairs += 1
            same_labels += ol == nl
            label_old.update(ol)
            label_new.update(nl)
        if old != new or ol != nl:
            rows.append({"case_id": c["case_id"], "bank": c["bank"], "old_decision": old, "new_decision": new,
                         "old_labels": "|".join(ol), "new_labels": "|".join(nl),
                         "old_errors": c.get("ref_errors", ""), "new_error": r.get("error_code") or "",
                         "content_result": r.get("content_result") or "",
                         "refusal_result": r.get("refusal_result") or "",
                         "new_reason": r.get("reason") or r.get("error_detail") or "",
                         "question": c["question"], "answer": c["exposed_output"]})
    summary = {"cases": len(by_id), "judged": len(recs), "missing_from_run": len(missing),
               "decision_old_to_new": dict(sorted(cross.items())),
               "by_bank": {b: dict(v) for b, v in sorted(per_bank.items())},
               "old_error_types_now": {k: dict(v) for k, v in sorted(old_errors.items())},
               "new_error_codes": dict(new_errors),
               "labels_both_judged": label_pairs, "labels_identical": same_labels,
               "label_counts_old": dict(label_old.most_common()), "label_counts_new": dict(label_new.most_common()),
               "differences": len(rows)}
    return summary, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("run")
    ap.add_argument("--diff")
    args = ap.parse_args()
    cases = [json.loads(l) for l in open(args.cases, encoding="utf-8") if l.strip()]
    recs = [json.loads(l) for l in open(args.run, encoding="utf-8") if l.strip()]
    summary, rows = compare(cases, recs)
    if args.diff and rows:
        with open(args.diff, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
