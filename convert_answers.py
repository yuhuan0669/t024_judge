"""Convert a T-024 answers CSV into judge input JSONL.

    python3 convert_answers.py answers.csv data/full.jsonl
    python3 convert_answers.py answers.csv data/pilot.jsonl --pilot

CSV columns used: question_id, repeat, bank, question, answer; the old judge's
columns (raw_judge_decision, bank_decision, risk_decision, risk_labels,
errors) are carried as ref_* fields for comparison and are never sent to the
model. case_id is "<question_id>#<repeat>".

--pilot picks a fixed, stratified sample (seeded, reproducible): every row the
old judge lost to CONTENT_FILTERED, a few rows per other old error type, and
old FAIL / PENDING / PASS rows spread over the three banks.

Input data and the generated JSONL stay out of git (see .gitignore).
"""
import argparse
import csv
import json
import random
import sys
from collections import Counter

REF_COLUMNS = ("raw_judge_decision", "bank_decision", "risk_decision", "risk_labels", "errors")
SEED = 20261027
# old error type (substring of the errors column) -> rows to take; None = all
PILOT_ERRORS = {"CONTENT_FILTERED": None, "ungrounded_quote": 20, "a5_unconfirmed_risk": 20,
                "nonrefusal_fail_behavior_conflict": 20, "pending_pass": 20, "TRANSPORT_ERROR": 20}
# (old bank_decision, bank) -> rows to take
PILOT_DECISIONS = {("FAIL", "generation"): 20, ("FAIL", "refusal"): 15, ("FAIL", "nonrefusal"): 15,
                   ("PENDING", "generation"): 15, ("PENDING", "refusal"): 5, ("PENDING", "nonrefusal"): 6,
                   ("PASS", "generation"): 30, ("PASS", "refusal"): 15, ("PASS", "nonrefusal"): 15}


def to_case(row):
    case = {"case_id": f"{row['question_id']}#{row['repeat']}", "bank": row["bank"],
            "question": row["question"], "exposed_output": row["answer"]}
    case.update({f"ref_{k}": row.get(k, "") for k in REF_COLUMNS})
    return case


def pick_pilot(rows):
    rng = random.Random(SEED)
    chosen, seen = [], set()

    def take(pool, n):
        pool = [r for r in pool if id(r) not in seen]
        picked = pool if n is None or len(pool) <= n else rng.sample(pool, n)
        for r in picked:
            seen.add(id(r))
        chosen.extend(picked)

    for err, n in PILOT_ERRORS.items():
        take([r for r in rows if err in r["errors"]], n)
    done = [r for r in rows if r["judge_state"] == "DONE"]
    for (decision, bank), n in PILOT_DECISIONS.items():
        take([r for r in done if r["bank_decision"] == decision and r["bank"] == bank], n)
    return sorted(chosen, key=lambda r: (r["question_id"], r["repeat"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("out")
    ap.add_argument("--pilot", action="store_true")
    args = ap.parse_args()
    csv.field_size_limit(10 ** 9)
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if args.pilot:
        rows = pick_pilot(rows)
    cases = [to_case(r) for r in rows]
    ids = Counter(c["case_id"] for c in cases)
    dup = [k for k, v in ids.items() if v > 1]
    if dup:
        sys.exit(f"duplicate case_id: {dup[:5]}")
    with open(args.out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(json.dumps({"cases": len(cases), "by_bank": dict(Counter(c["bank"] for c in cases)),
                      "old_bank_decision": dict(Counter(c["ref_bank_decision"] for c in cases)),
                      "answer_chars": sum(len(c["exposed_output"]) for c in cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
