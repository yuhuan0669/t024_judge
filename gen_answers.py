"""Generate answers from the tested model for judge input (A/B tests of the
tested model's system prompt).

    python3 gen_answers.py data/pilot.jsonl data/ab_A.jsonl --url http://host/v1/chat/completions \
        --params config.json [--system system.txt] [--variant A] [--workers 16] [--max-requests 300] [--resume]

--params  JSON file with the generation parameters, given directly or under
          "generation_parameters" (the T-024 run config.json works as is); sent
          unchanged, with stream forced off.
--system  system prompt file; omitted = no system message (the model was
          fine-tuned without one).

The endpoint is called with judge.Client's retry policy, deadline and request
budget; LORA_KEY is sent as a bearer token when set. Each output line keeps
case_id, bank and question, the answer as exposed_output (ready for judge.py)
and a gen block: variant, system_sha, params_sha, served model,
finish_reason, token usage, latency, failed attempts, or the error when the
call failed (exposed_output is then empty and judge.py reports EMPTY_OUTPUT).
The old judge's ref_* fields are not copied. --resume keeps the lines of an
earlier run of the same output that have an answer.
"""
import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import judge as j


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_params(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return dict(data.get("generation_parameters", data))


def build_body(params, system_text, question):
    body = {k: v for k, v in params.items() if k != "messages"}
    body["stream"] = False
    messages = [] if system_text is None else [{"role": "system", "content": system_text}]
    body["messages"] = messages + [{"role": "user", "content": question}]
    return body


def generate(case, client, params, system_text, meta):
    gen = dict(meta)
    out = {"case_id": case["case_id"], "bank": case["bank"], "question": case["question"],
           "exposed_output": "", "gen": gen}
    body = build_body(params, system_text, case["question"])
    try:
        resp, secs, failed = client.post(body, lambda data: j.parse_chat_response(data, body.get("max_tokens")))
    except j.ApiError as ex:
        gen.update(error=str(ex), failed_attempts=ex.attempts)
        return out
    out["exposed_output"] = resp["content"]
    gen.update(served_model=resp["model"], finish_reason=resp["finish_reason"],
               prompt_tokens=resp["prompt_tokens"], completion_tokens=resp["completion_tokens"],
               secs=secs, failed_attempts=failed)
    if resp["reasoning"]:
        gen["reasoning_content"] = resp["reasoning"]
    return out


def run(cases, client, params, system_text, variant, out_path, workers=16, resume=False):
    done = {}
    if resume and os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("exposed_output"):
                    done[r["case_id"]] = r
    meta = {"variant": variant, "system_sha": sha(system_text) if system_text is not None else "none",
            "params_sha": sha(json.dumps(params, sort_keys=True, ensure_ascii=False)),
            "model_requested": params.get("model")}
    recs = list(done.values())
    with open(out_path, "w", encoding="utf-8") as f, ThreadPoolExecutor(workers) as pool:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        futures = [pool.submit(generate, c, client, params, system_text, meta)
                   for c in cases if c["case_id"] not in done]
        for fut in as_completed(futures):
            r = fut.result()
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            recs.append(r)
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("out")
    ap.add_argument("--url", required=True, help="full chat-completions URL of the tested model")
    ap.add_argument("--params", required=True)
    ap.add_argument("--system")
    ap.add_argument("--variant", default="")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    params = load_params(args.params)
    system_text = None
    if args.system:
        with open(args.system, encoding="utf-8") as f:
            system_text = f.read().strip()
    with open(args.cases, encoding="utf-8") as f:
        cases = [json.loads(l) for l in f if l.strip()]
    base = args.url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    budget = j.HttpBudget(args.max_requests) if args.max_requests else None
    client = j.Client(base=base, key=os.environ.get("LORA_KEY", ""), model=params.get("model"),
                      timeout=args.timeout, deadline=args.timeout * 2, budget=budget)
    recs = run(cases, client, params, system_text, args.variant, args.out, args.workers, args.resume)
    finish = {}
    for r in recs:
        k = r["gen"].get("finish_reason") or ("ERROR" if r["gen"].get("error") else None)
        finish[str(k)] = finish.get(str(k), 0) + 1
    answered = [r for r in recs if r["exposed_output"]]
    print(json.dumps({"variant": args.variant, "cases": len(recs), "answered": len(answered),
                      "finish_reason": finish, "system_sha": recs[0]["gen"]["system_sha"] if recs else None,
                      "http_requests": budget.used if budget else None,
                      "avg_answer_chars": round(sum(len(r["exposed_output"]) for r in answered) / len(answered))
                      if answered else None}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
