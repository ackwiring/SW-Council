"""Unit tests for scripts/council_core.py: python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import council_core as core  # noqa: E402

AUTH_DIFF = """diff --git a/src/auth/login.ts b/src/auth/login.ts
index 1..2 100644
--- a/src/auth/login.ts
+++ b/src/auth/login.ts
@@ -1 +1,3 @@
+export const login = (password: string, token: string) => checkSession(token);
+const sql = `select * from users where name = '${name}'`;
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
+Docs for the login flow.
"""


class LensSelection(unittest.TestCase):
    def test_changed_files_and_split(self):
        self.assertEqual(core.changed_files(AUTH_DIFF), ["src/auth/login.ts", "README.md"])
        parts = core.split_diff(AUTH_DIFF)
        self.assertEqual(list(parts), ["src/auth/login.ts", "README.md"])
        self.assertIn("checkSession", parts["src/auth/login.ts"])

    def test_auth_code_brings_in_security_and_keeps_three_or_four_lenses(self):
        lenses = core.select_lenses(AUTH_DIFF, max_lenses=4)
        self.assertEqual(lenses[0], "security")
        self.assertIn("contrarian", lenses)
        self.assertTrue(3 <= len(lenses) <= 4)

    def test_dropped_and_requested_lenses(self):
        self.assertNotIn("security", core.select_lenses(AUTH_DIFF, dropped=("security",)))
        self.assertEqual(core.select_lenses(AUTH_DIFF, requested=["dx", "dx", "outsider"]), ["dx", "outsider"])
        with self.assertRaises(ValueError):
            core.select_lenses(AUTH_DIFF, requested=["nope"])

    def test_a_dull_diff_still_gets_the_minimum(self):
        dull = "diff --git a/x.txt b/x.txt\n+hello\n"
        self.assertEqual(len(core.select_lenses(dull, max_lenses=4)), 3)
        self.assertEqual(len(core.select_lenses(dull, max_lenses=2)), 2)

    def test_big_diffs_ask_first_principles(self):
        big = "diff --git a/a.py b/a.py\n" + "+x = 1\n" * 500
        self.assertIn("first_principles", core.select_lenses(big, max_lenses=4))


class Context(unittest.TestCase):
    def test_rules_and_report_come_first_and_the_diff_is_what_gets_cut(self):
        text = core.context_block(AUTH_DIFF, "layers: {}", {"verdict": "green"}, budget_chars=10_000)
        self.assertLess(text.index("<rules>"), text.index("<deterministic-report>"))
        self.assertLess(text.index("<deterministic-report>"), text.index('<file-diff path="src/auth/login.ts">'))
        tight = core.context_block(AUTH_DIFF, "layers: {}", None, budget_chars=200)
        self.assertIn("<rules>", tight)
        self.assertIn('truncated="true"', tight)
        self.assertIn("more file(s) not shown", tight)

    def test_no_rules_no_report_or_unsplittable_diff(self):
        self.assertIn('path="(diff)"', core.context_block("plain text", None, None, 1000))
        self.assertEqual(core.context_block("", None, None, 1000), "")


class StructuredFindings(unittest.TestCase):
    def test_parse_json_object_from_plain_fenced_and_chatty_answers(self):
        self.assertEqual(core.parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(core.parse_json_object('Sure!\n```json\n{"a": "}"}\n```'), {"a": "}"})
        self.assertEqual(core.parse_json_object('Here: {"a": {"b": "x\\"y"}} done'), {"a": {"b": 'x"y'}})
        self.assertIsNone(core.parse_json_object("no json here"))
        self.assertIsNone(core.parse_json_object("{broken"))
        self.assertIsNone(core.parse_json_object(""))
        self.assertEqual(core.parse_json_object('[1] then {"ok": true}'), {"ok": True})

    def test_normalise_drops_junk_and_fixes_severity(self):
        raw = {"findings": [
            {"file": "a.ts", "line": 3, "severity": "HIGH", "message": "SQL built from input", "suggested_fix": "bind"},
            {"file": None, "line": -2, "severity": "urgent", "message": "vague"},
            {"message": ""},
            "junk",
        ]}
        out = core.normalise_findings(raw, "security")
        self.assertEqual([f["severity"] for f in out], ["high", "medium"])
        self.assertEqual(out[1]["line"], None)
        self.assertEqual(out[0]["lens"], "security")
        self.assertEqual(core.normalise_findings({"findings": "nope"}, "dx"), [])
        self.assertEqual(core.normalise_findings(None, "dx"), [])

    def test_merge_keeps_one_finding_per_issue_with_every_lens(self):
        a = {"file": "a.ts", "line": 3, "lens": "security", "severity": "medium",
             "message": "SQL built from user input here", "suggested_fix": None}
        b = {**a, "lens": "contrarian", "line": 4, "severity": "critical", "suggested_fix": "bind params"}
        c = {**a, "lens": "dx", "message": "Name is unclear"}
        merged = core.merge_findings([a, b, c, dict(a)])
        self.assertEqual(len(merged), 2)
        # Reworded by another model, still the same issue.
        reworded = {**a, "lens": "performance", "message": "SQL is built from raw user input here"}
        far = {**a, "lens": "outsider", "line": 40}
        unknown_line = {**a, "lens": "executor", "line": None}
        self.assertEqual(len(core.merge_findings([a, reworded])), 1)
        self.assertEqual(len(core.merge_findings([a, far, unknown_line])), 3)
        blank = {**a, "message": "!!"}
        self.assertEqual(len(core.merge_findings([blank, dict(blank, lens="dx")])), 1)
        self.assertEqual(merged[0]["lenses"], ["security", "contrarian"])
        self.assertEqual(merged[0]["severity"], "critical")
        self.assertEqual(merged[0]["suggested_fix"], "bind params")
        self.assertEqual([f["id"] for f in merged], ["F1", "F2"])

    def test_lenses_agree(self):
        one = core.merge_findings([{"file": "a", "line": 1, "lens": "dx", "severity": "low",
                                    "message": "m", "suggested_fix": None}])
        self.assertTrue(core.lenses_agree([], 3))
        self.assertTrue(core.lenses_agree(one, 1))
        self.assertFalse(core.lenses_agree(one, 3))
        one[0]["lenses"].append("security")
        self.assertTrue(core.lenses_agree(one, 3))


class PeerAndChair(unittest.TestCase):
    def test_parse_ranking(self):
        ids = ["F1", "F2", "F3"]
        self.assertEqual(core.parse_ranking('{"ranking": ["f2", "F9", "F2", "F1"]}', ids), ["F2", "F1"])
        self.assertEqual(core.parse_ranking("1. F3\n2. F1", ids), ["F3", "F1"])

    def test_borda(self):
        order = core.borda([["F2", "F1"], ["F2", "F3", "F1"]], ["F1", "F2", "F3"])
        self.assertEqual([i for i, _ in order], ["F2", "F1", "F3"])
        self.assertEqual(order[0], ("F2", 1.0))
        self.assertEqual(core.borda([], []), [])

    def test_parse_ruling(self):
        r = core.parse_ruling('{"ruling": "Reject", "justification": "SQLi", "action_plan": ["bind"], '
                              '"blocking_finding_ids": ["f1"]}')
        self.assertEqual(r, {"ruling": "reject", "justification": "SQLi", "action_plan": ["bind"],
                             "blocking_finding_ids": ["F1"]})
        self.assertEqual(core.parse_ruling('{"ruling": "approve"}')["ruling"], "approve_with_conditions")
        self.assertIsNone(core.parse_ruling('{"ruling": "maybe"}'))
        self.assertIsNone(core.parse_ruling("COUNCIL_VERDICT: APPROVED"))

    def test_output_record_marks_blocking_only_on_reject(self):
        merged = core.merge_findings([{"file": "a", "line": 1, "lens": "security", "severity": "high",
                                       "message": "m", "suggested_fix": None}])
        reject = {"ruling": "reject", "justification": "j", "action_plan": [], "blocking_finding_ids": ["F1"]}
        rec = core.output_record(reject, merged, ["security"], {"chairman": "x"}, {})
        self.assertEqual(rec["schema"], "sw-council.ruling/v1")
        self.assertTrue(rec["findings"][0]["blocking"])
        self.assertEqual(rec["findings"][0]["lens"], "security")
        approve = {**reject, "ruling": "approve_with_conditions"}
        self.assertFalse(core.output_record(approve, merged, [], {}, {})["findings"][0]["blocking"])


class CacheAndConfig(unittest.TestCase):
    def test_cache_round_trip_ttl_and_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            cache = core.Cache(d)
            key = core.cache_key("diff", "rules", "security", "m")
            self.assertIsNone(cache.get(key))
            cache.put(key, [{"message": "x"}])
            self.assertEqual(cache.get(key), [{"message": "x"}])
            self.assertEqual(cache.hits, 1)
            self.assertNotEqual(key, core.cache_key("diff", "rules", "security", "other-model"))
            stale = core.Cache(d, ttl_seconds=0)
            time.sleep(0.01)
            self.assertIsNone(stale.get(key))
            off = core.Cache(d, enabled=False)
            off.put("k2", [])
            self.assertIsNone(off.get(key))
            self.assertFalse(os.path.exists(os.path.join(d, "k2.json")))

    def test_config_defaults_overrides_and_validation(self):
        self.assertEqual(core.load_config(None)["max_lenses"], 4)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w", encoding="utf-8") as h:
                json.dump({"_comment": "x", "chairman_model": "fake:chair", "peer_review": "off"}, h)
            cfg = core.load_config(path)
            self.assertEqual((cfg["chairman_model"], cfg["peer_review"]), ("fake:chair", "off"))
            self.assertNotIn("_comment", cfg)
            for bad in ({"lens_models": []}, {"peer_review": "sometimes"}):
                with open(path, "w", encoding="utf-8") as h:
                    json.dump(bad, h)
                with self.assertRaises(ValueError):
                    core.load_config(path)
            with open(path, "w", encoding="utf-8") as h:
                json.dump([1], h)
            with self.assertRaises(ValueError):
                core.load_config(path)

    def test_route_and_assign(self):
        self.assertEqual(core.route("gemini:gemini-2.5-pro"), ("gemini", "gemini-2.5-pro"))
        self.assertEqual(core.route("ollama:qwen2.5-coder:latest"), ("ollama", "qwen2.5-coder:latest"))
        self.assertEqual(core.route("qwen2.5-coder:latest"), (None, "qwen2.5-coder:latest"))
        self.assertEqual(core.assign_models(["a", "b", "c"], ["m1", "m2"]), {"a": "m1", "b": "m2", "c": "m1"})


if __name__ == "__main__":
    unittest.main()
