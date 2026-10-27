"""Regression: v3 prompt (code decides), optionally v2 (model decides).

    DS_BASE=... DS_KEY=... python3 regress.py [--runs 3] [--workers 4] [--versions v2,v3]
                                              [--cases other_cases.jsonl]

--cases defaults to cases_regress.json; a JSON array or a .jsonl file both work.
A run is correct when the decision matches and the risk labels contain every
expect_labels code and nothing outside expect_labels + allow_labels. Cases
without expect_decision (e.g. samples taken from a test bank to see how the
judge copes) are run and reported but not scored.
"""
import argparse
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import judge as j

HERE = Path(__file__).parent
V2_INPUT_KEYS = ("bank", "a5_applicability", "question", "exposed_output")


def run_v2(case, client, prompt):
    user = json.dumps({k: case[k] for k in V2_INPUT_KEYS}, ensure_ascii=False)
    try:
        resp = client.chat([{"role": "system", "content": prompt}, {"role": "user", "content": user}])
    except Exception as ex:
        return {"calls": [], "issues": [f"{type(ex).__name__}: {ex}"], "decision": "ERROR", "error_code": "API_ERROR"}
    rec = {"calls": [resp], "issues": []}
    try:
        out = j.parse_json(resp["content"])
    except ValueError as ex:
        rec.update(decision="NO_OUTPUT", issues=[f"JSON解析失败({resp['finish_reason']}): {ex}"])
        return rec
    c = out.get("checks", {})
    present = sorted(k for k, v in c.items() if v == "PRESENT")
    labels = sorted(out.get("risk_labels") or [])
    if sorted(c) != j.CODES:
        rec["issues"].append("checks键不全")
    if present != labels:
        rec["issues"].append(f"PRESENT{present}≠risk_labels{labels}")
    if labels and out.get("decision") != "FAIL":
        rec["issues"].append(f"有成立风险却判{out.get('decision')}")
    for e in out.get("evidence", []):
        if j._norm(e.get("quote", "")) not in j._norm(case["exposed_output"]) or not e.get("quote"):
            rec["issues"].append("引文不在原文")
    rec.update(decision=out.get("decision"), risk_labels=labels, behavior=out.get("behavior"),
               a5_pattern=f"a5={out.get('a5')},R30={c.get('R30')}")
    return rec


def run_v3(case, client, prompt, sha, service_sha, config, params):
    inp = {k: v for k, v in case.items() if not k.startswith(("expect_", "allow_"))}
    rec = j.judge(inp, client, prompt, sha, max_repairs=config.get("max_repairs", 1),
                  service_sha=service_sha, approved_models=config.get("approved_models"), params=params)
    c = rec.get("checks") or {}
    rec["a5_pattern"] = f"a5={case.get('a5_applicability')}(输入),R30={c.get('R30')},R31={c.get('R31')}"
    rec["issues"] = rec.get("errors") or ([rec["error_code"]] if rec.get("error_code") else [])
    return rec


def expected_decisions(case):
    d = case.get("expect_decision")
    if d is None:
        return []
    return d if isinstance(d, list) else [d]


def is_correct(rec, case):
    """True / False, or None when the case carries no expectation."""
    if "expect_decision" not in case:
        return None
    labels = set(rec.get("risk_labels") or [])
    need = set(case.get("expect_labels", []))
    return (rec.get("decision") in expected_decisions(case) and need <= labels
            and labels <= need | set(case.get("allow_labels", [])))


def load_cases(path):
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".jsonl"):
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    return json.loads(text)


def shown_decision(r):
    d = str(r.get("decision"))
    if r.get("decision") == "ERROR":
        return f"{d}({r.get('error_code')})"
    return d + str(r.get("risk_labels") or "")


def summarize(recs):
    groups = defaultdict(list)
    for r in recs:
        groups[(r["case_id"], r["version"])].append(r)
    rows = []
    totals = defaultdict(lambda: defaultdict(float))
    order = {cid: i for i, cid in enumerate(dict.fromkeys(r["case_id"] for r in recs))}
    for (cid, ver), rs in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
        calls = [c for r in rs for c in r["calls"]]
        rt = sum(c.get("reasoning_tokens") or 0 for c in calls) / len(rs)
        secs = sum(c.get("secs") or 0 for c in calls) / len(rs)
        scored = [r for r in rs if r.get("correct") is not None]
        ok = sum(bool(r["correct"]) for r in scored)
        issues = sum(len(r["issues"]) for r in rs)
        expect = rs[0].get("expect") or "-"
        rows.append(f"| {cid} | {ver} | {expect} | {' / '.join(shown_decision(r) for r in rs)} | "
                    f"{f'{ok}/{len(scored)}' if scored else '-'} | {' / '.join(str(r.get('behavior')) for r in rs)} | "
                    f"{'; '.join(sorted(set(r['a5_pattern'] for r in rs if 'a5_pattern' in r)))} | {issues} | "
                    f"{sum(r.get('repairs', 0) for r in rs)} | {rt:.0f} | {secs:.1f} |")
        t = totals[ver]
        t["n"] += len(rs); t["scored"] += len(scored); t["ok"] += ok; t["issues"] += issues
        t["rt"] += rt * len(rs); t["secs"] += secs * len(rs)
        t["prompt"] += sum(c.get("prompt_tokens") or 0 for c in calls)
    lines = ["| 用例 | 版本 | 预期 | 判定 | 正确 | 行为 | R30/R31处理 | 校验问题 | 修复轮 | 平均推理token | 平均耗时s |",
             "|---|---|---|---|---|---|---|---|---|---|---|"] + rows
    lines += ["", "| 版本 | 正确率 | 校验问题合计 | 平均推理token | 平均耗时s | 平均输入token |", "|---|---|---|---|---|---|"]
    for ver, t in sorted(totals.items()):
        lines.append(f"| {ver} | {t['ok']:.0f}/{t['scored']:.0f} | {t['issues']:.0f} | {t['rt'] / t['n']:.0f} | "
                     f"{t['secs'] / t['n']:.1f} | {t['prompt'] / t['n']:.0f} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--summarize", help="only rebuild the markdown table from an existing results json")
    ap.add_argument("--versions", default="v3")
    ap.add_argument("--cases", default=str(HERE / "cases_regress.json"))
    args = ap.parse_args()
    if args.summarize:
        src = Path(args.summarize)
        summary = summarize(json.loads(src.read_text(encoding="utf-8")))
        src.with_suffix(".md").write_text(summary + "\n", encoding="utf-8")
        print(summary)
        return
    service, service_sha = j.load_service()
    config, config_sha = j.load_config()
    cases = [j.apply_service(c, service) for c in load_cases(args.cases)]
    p2 = (HERE / "archive" / "prompt_v2.txt").read_text(encoding="utf-8")
    p3, sha3 = j.load_prompt()
    req = config.get("request", {})
    client = j.Client(model=req.get("model"), max_tokens=req.get("max_tokens", 16000),
                      temperature=req.get("temperature", 0))
    params = j.judge_params(config, config_sha, client)

    jobs = [(v, c, r) for v in args.versions.split(",") for c in cases for r in range(args.runs)]

    def work(job):
        version, case, run = job
        if version == "v2":
            rec = run_v2(case, client, p2)
        else:
            rec = run_v3(case, client, p3, sha3, service_sha, config, params)
        expect = "/".join(expected_decisions(case)) + (str(case["expect_labels"]) if case.get("expect_labels") else "")
        if case.get("allow_labels"):
            expect += f"(可含{','.join(case['allow_labels'])})"
        rec.update(version=version, case_id=case.get("case_id"), run=run, expect=expect,
                   correct=is_correct(rec, case))
        mark = {True: "ok", False: "WRONG", None: "-"}[rec["correct"]]
        print(version, case.get("case_id"), run, shown_decision(rec), rec.get("behavior"), mark,
              rec["issues"], flush=True)
        return rec

    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        recs = list(pool.map(work, jobs))
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / f"regress_{stamp}.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = summarize(recs)
    (out_dir / f"regress_{stamp}.md").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    print(f"wall {time.time() - t0:.0f}s; served models: {sorted(set(str(c.get('model')) for r in recs for c in r['calls']))}; "
          f"params: {json.dumps(params, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
