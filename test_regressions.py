"""Offline regression coverage for the seven v3.8 review findings.

Every case and reviewer below is a synthetic fixture. HTTP calls are mocked;
socket connections are disabled as an additional guard against live calls.
"""
import contextlib
import copy
import email.utils
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

import judge as j
import regress
import report
import review
from test_judge import CaseClient, FakeHTTPResponse


SERVICE = {"a5_applicability": "APPLICABLE", "service_profile": "Synthetic service",
           "a5_min_help": ["Synthetic requirement"]}


def case(cid="q#1", **overrides):
    return dict({"case_id": cid, "bank": "generation", "question": "Synthetic question",
                 "exposed_output": "Synthetic answer"}, **overrides)


def reply():
    return json.dumps({"behavior": "ANSWER", "harmful_help": "NOT_APPLICABLE",
                       "checks": {code: "ABSENT" for code in j.CODES}, "evidence": [],
                       "pending": [], "reason": "Synthetic test fixture"})


def params(**overrides):
    config, sha = j.load_config()
    client = j.Client(base=overrides.pop("base", "https://gateway.invalid/v1"), key="test-only",
                      model=overrides.pop("model", "synthetic-model"), **overrides)
    return j.judge_params(config, sha, client)


def machine_record(c):
    rec = j.new_record(j.apply_service(c, SERVICE), "prompt-sha", "service-sha", params())
    rec.update(decision="FAIL", content_result="FAIL", refusal_result="NOT_APPLICABLE",
               risk_labels=["R07"], fail_reasons=["RISK:R07"], hold_reasons=[],
               reason="Synthetic test fixture", evidence=[], pending=[])
    return rec


def human_pass(row):
    return dict(row, human_content_result="PASS", human_refusal_result="",
                human_risk_labels="", reviewer="SYNTHETIC_TEST_ONLY",
                note="Test fixture; not an actual human review.")


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        for name in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, name, side_effect=AssertionError("Network disabled"))
            patcher.start()
            self.addCleanup(patcher.stop)


class ResumeRegression(OfflineTestCase):
    def run_case(self, path, c, request_params=None, resume=False):
        client = CaseClient({c["case_id"]: reply()})
        recs = j.run_batch([json.dumps(c)], client, "prompt", "prompt-sha", SERVICE,
                           "service-sha", str(path), workers=1, params=request_params or params(), resume=resume)
        return recs[0], client.asked

    def test_changed_row_service_settings_are_rejudged(self):
        changes = [({"a5_applicability": "UNCONFIRMED"}, {"a5_applicability": "APPLICABLE"}),
                   ({}, {"service_profile": "Different service"}),
                   ({}, {"a5_min_help": ["Different requirement"]})]
        for before, after in changes:
            with self.subTest(after=after), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "out.jsonl"
                old, _ = self.run_case(path, case(**before))
                new, calls = self.run_case(path, case(**after), resume=True)
                self.assertEqual(calls, ["q#1"])
                self.assertFalse(new.get("reused"))
                self.assertEqual(new["a5_applicability"], "APPLICABLE")
                self.assertEqual(new["checks"]["R31"], "ABSENT")
                self.assertNotEqual(new["evaluation_sha"], old["evaluation_sha"])
                previous = json.loads(Path(str(path) + ".superseded.jsonl").read_text(encoding="utf-8"))
                self.assertEqual(previous, old)

    def test_changed_effective_request_parameters_are_rejudged(self):
        for changed in ({"model": "another-model"}, {"max_tokens": 32000},
                        {"base": "https://another.invalid/v1"}, {"temperature": 0.5}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "out.jsonl"
                old, _ = self.run_case(path, case())
                new, calls = self.run_case(path, case(), params(**changed), resume=True)
                self.assertEqual(calls, ["q#1"])
                self.assertNotEqual(new["evaluation_sha"], old["evaluation_sha"])

    def test_same_effective_input_reuses_defaults_and_ignores_reference_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.jsonl"
            old, _ = self.run_case(path, case())
            explicit = dict(case(), **SERVICE, ref_bank_decision="PASS")
            new, calls = self.run_case(path, explicit, resume=True)
            self.assertEqual(calls, [])
            self.assertTrue(new["reused"])
            self.assertEqual(new["evaluation_sha"], old["evaluation_sha"])

    def test_record_without_evaluation_fingerprint_is_not_reused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.jsonl"
            old, _ = self.run_case(path, case())
            old.pop("evaluation_sha", None)
            path.write_text(json.dumps(old) + "\n", encoding="utf-8")
            new, calls = self.run_case(path, case(), resume=True)
            self.assertEqual(calls, ["q#1"])
            self.assertFalse(new.get("reused"))

    def test_endpoint_identity_does_not_record_credentials(self):
        endpoint = "https://synthetic-user:test-only@gateway.invalid/v1?token=test-only"
        actual = params(base=endpoint)
        self.assertIn("endpoint_sha", actual)
        self.assertNotIn("test-only", json.dumps(actual))
        self.assertNotEqual(actual["endpoint_sha"], params()["endpoint_sha"])


class ReviewRegression(OfflineTestCase):
    def test_export_rejects_different_question_answer_bank_or_reasoning(self):
        original = case()
        rec = machine_record(original)
        for changed in ({"question": "Different question"}, {"exposed_output": "Different answer"},
                        {"bank": "nonrefusal"}, {"visible_reasoning": "Different visible reasoning"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "input_sha"):
                review.export_rows([rec], [dict(original, **changed)])

    def test_export_requires_unique_matching_case_ids_when_cases_are_supplied(self):
        c = case()
        rec = machine_record(c)
        for cases in ([], [case("different#1")], [c, c]):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                review.export_rows([rec], cases)
        self.assertEqual(review.export_rows([rec], [c])[0]["answer"], c["exposed_output"])
        self.assertEqual(review.export_rows([rec])[0]["answer"], "")  # --cases remains optional

    def test_export_cli_does_not_write_a_mismatched_review_table(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            records, cases, target = root / "out.jsonl", root / "cases.jsonl", root / "review.csv"
            records.write_text(json.dumps(machine_record(case())) + "\n", encoding="utf-8")
            cases.write_text(json.dumps(case(exposed_output="Different answer")) + "\n", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["review.py", "export", str(records), str(target),
                                                  "--cases", str(cases)]), contextlib.redirect_stdout(io.StringIO()):
                code = review.main()
            self.assertEqual(code, 2)
            self.assertFalse(target.exists())

    def test_incremental_and_empty_apply_preserve_prior_human_verdicts(self):
        cases = [case("a#1"), case("b#1")]
        recs = [machine_record(c) for c in cases]
        rows = review.export_rows(recs, cases)
        first, _ = review.apply_review(recs, [human_pass(rows[0])])
        second, summary = review.apply_review(first, [human_pass(rows[1])])
        self.assertEqual(summary["rejected"], [])
        self.assertEqual(second[0], first[0])
        self.assertEqual(report.build(second)["reviewed_by_human"], 2)
        self.assertEqual([r["final"]["decision"] for r in second], ["PASS", "PASS"])
        unchanged, _ = review.apply_review(second, [])
        self.assertEqual(unchanged, second)

    def test_rejected_row_does_not_revoke_existing_human_review(self):
        c = case()
        row = human_pass(review.export_rows([machine_record(c)], [c])[0])
        first, _ = review.apply_review([machine_record(c)], [row])
        second, summary = review.apply_review(first, [dict(row, input_sha="stale")])
        self.assertEqual(len(summary["rejected"]), 1)
        self.assertEqual(second, first)

    def test_review_from_different_evaluation_settings_is_rejected(self):
        c = case()
        old = machine_record(c)
        row = human_pass(review.export_rows([old], [c])[0])
        changed = machine_record(dict(c, service_profile="Different service"))
        self.assertEqual(old["input_sha"], changed["input_sha"])
        final, summary = review.apply_review([changed], [row])
        self.assertEqual(summary["applied"], 0)
        self.assertIn("evaluation_sha", summary["rejected"][0]["problems"][0])
        self.assertEqual(final[0]["final"]["source"], "judge")


class BatchRegression(OfflineTestCase):
    def test_bad_id_types_do_not_interrupt_valid_cases(self):
        for invalid in ([], {}, None, "", "  ", 17):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as d:
                client = CaseClient({"ok#1": reply()})
                recs = j.run_batch([json.dumps(case("ok#1")), json.dumps(case(invalid))], client,
                                   "prompt", "sha", SERVICE, "ssha", str(Path(d) / "out.jsonl"), workers=1)
                by_seq = {r["seq"]: r for r in recs}
                self.assertEqual(client.asked, ["ok#1"])
                self.assertEqual(by_seq[1]["decision"], "PASS")
                self.assertEqual(by_seq[2]["error_code"], "INVALID_INPUT")


class RegressionBudget(OfflineTestCase):
    def test_cli_and_config_budgets_including_zero_limit_actual_http_attempts(self):
        config, sha = j.load_config()
        real_client = j.Client
        for cli, configured, expected in ((0, None, 0), (None, 0, 0), (None, 1, 1),
                                          (1, 0, 1), (None, None, 2)):
            with self.subTest(cli=cli, configured=configured), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "archive").mkdir()
                (root / "archive" / "prompt_v2.txt").write_text("Unused v2 test prompt", encoding="utf-8")
                source = root / "cases.json"
                source.write_text(json.dumps([case("a#1"), case("b#1")]), encoding="utf-8")
                cfg = copy.deepcopy(config)
                cfg["max_requests_per_batch"] = configured
                argv = ["regress.py", "--cases", str(source), "--runs", "1", "--workers", "1"]
                if cli is not None:
                    argv += ["--max-requests", str(cli)]
                payload = {"model": cfg["approved_models"][0], "choices": [
                    {"message": {"content": reply()}, "finish_reason": "stop"}], "usage": {}}
                def client_factory(**kw):
                    return real_client(base="https://gateway.invalid/v1", key="test-only", **kw)
                with mock.patch.object(regress, "HERE", root), mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(j, "Client", side_effect=client_factory), \
                        mock.patch.object(j, "load_config", return_value=(cfg, sha)), \
                        mock.patch.object(j.urllib.request, "urlopen", side_effect=lambda *a, **k: FakeHTTPResponse(payload)) as http, \
                        contextlib.redirect_stdout(io.StringIO()):
                    regress.main()
                self.assertEqual(http.call_count, expected)
                recs = json.loads(next((root / "results").glob("*.json")).read_text(encoding="utf-8"))
                self.assertEqual(sum(r.get("error_code") == "BUDGET_EXCEEDED" for r in recs), 2 - expected)


class HttpRegression(OfflineTestCase):
    def test_retry_after_longer_than_local_cap_is_respected(self):
        for header in ("120", email.utils.formatdate(220, usegmt=True)):
            with self.subTest(header=header):
                client = j.Client(base="https://gateway.invalid", key="test-only")
                waits = []
                payload = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
                with mock.patch.object(j.time, "time", return_value=100), \
                        mock.patch.object(j.time, "sleep", side_effect=waits.append), \
                        mock.patch.object(j.urllib.request, "urlopen", side_effect=[
                            urllib.error.HTTPError("https://gateway.invalid", 429, "busy", {"Retry-After": header}, None),
                            FakeHTTPResponse(payload)]):
                    response = client.chat([{"role": "user", "content": "Synthetic"}])
                self.assertEqual(waits, [120])
                self.assertEqual(response["failed_attempts"][0]["wait_s"], 120)
        self.assertEqual(j.backoff_s(10), j.RETRY_WAIT_MAX_S)

    def test_nonfinite_retry_after_is_ignored(self):
        for value in ("NaN", "inf", "-inf"):
            with self.subTest(value=value):
                error = urllib.error.HTTPError("https://gateway.invalid", 429, "busy", {"Retry-After": value}, None)
                self.assertIsNone(j.retry_after_s(error))

    def test_malformed_response_is_logged_and_counted(self):
        malformed = [[], {"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]},
                     {"choices": [{"message": {"content": 123}}]},
                     {"choices": [{"message": {"content": "{}"}}], "usage": "invalid"},
                     {"choices": [{"message": {"content": "{}"}}], "usage": {"completion_tokens_details": []}}]
        for payload in malformed:
            with self.subTest(payload=payload):
                client = j.Client(base="https://gateway.invalid", key="test-only", budget=j.HttpBudget(1))
                with mock.patch.object(j.urllib.request, "urlopen", return_value=FakeHTTPResponse(payload)) as http:
                    rec = j.judge(j.apply_service(case(), SERVICE), client, "prompt", "sha")
                self.assertEqual((http.call_count, client.budget.used, j.http_requests(rec)), (1, 1, 1))
                self.assertEqual(rec["error_code"], "API_ERROR")
                self.assertEqual([a["kind"] for a in rec["failed_attempts"]], ["bad_response"])

    def test_malformed_response_retains_prior_failed_attempts(self):
        client = j.Client(base="https://gateway.invalid", key="test-only", budget=j.HttpBudget(2))
        with mock.patch.object(j.time, "sleep"), mock.patch.object(j.urllib.request, "urlopen", side_effect=[
                urllib.error.URLError("synthetic connection failure"),
                FakeHTTPResponse({"choices": [{"message": None}]})]):
            rec = j.judge(j.apply_service(case(), SERVICE), client, "prompt", "sha")
        self.assertEqual((client.budget.used, j.http_requests(rec)), (2, 2))
        self.assertEqual([a["kind"] for a in rec["failed_attempts"]], ["connection", "bad_response"])
        stats = j.run_stats([rec], 1, 1)
        self.assertEqual((stats["http_requests"], stats["failed_attempt_rate"]), (2, 1.0))


if __name__ == "__main__":
    unittest.main()
