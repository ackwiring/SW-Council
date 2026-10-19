"""End-to-end tests of sw_deliberate.py with the fake provider (no models, no network):
python -m unittest discover tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "sw_deliberate.py")

SQL_FINDING = {"file": "app/db.py", "line": 2, "severity": "high",
               "message": "SQL is built from user input with string formatting",
               "suggested_fix": "Use bound parameters."}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Council(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, "app"))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        with open(os.path.join(self.repo, "app", "db.py"), "w", encoding="utf-8") as h:
            h.write("def find(name):\n    return None\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        with open(os.path.join(self.repo, "app", "db.py"), "w", encoding="utf-8") as h:
            h.write("def find(name, password):\n    return f\"select * from users where name = '{name}'\"\n")
        self.config = os.path.join(self.tmp.name, "config.json")
        with open(self.config, "w", encoding="utf-8") as h:
            json.dump({"lens_models": ["fake:small-a", "fake:small-b"], "chairman_model": "fake:chair"}, h)
        self.log = os.path.join(self.tmp.name, "fake.log")
        self.cache = os.path.join(self.tmp.name, "cache")

    def tearDown(self):
        self.tmp.cleanup()

    def run_council(self, replies, *args):
        path = os.path.join(self.tmp.name, "replies.json")
        with open(path, "w", encoding="utf-8") as h:
            json.dump(replies, h)
        env = {**os.environ, "SW_COUNCIL_FAKE_RESPONSES": path, "SW_COUNCIL_FAKE_LOG": self.log,
               "SW_COUNCIL_CACHE_DIR": self.cache, "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": ""}
        env.pop("SW_COUNCIL_PROFILE", None)
        return subprocess.run([sys.executable, SCRIPT, "--config", self.config, *args], cwd=self.repo,
                              env=env, capture_output=True, text=True, encoding="utf-8")

    def sent(self):
        with open(self.log, encoding="utf-8") as h:
            return [json.loads(line) for line in h]

    def test_json_ruling_with_merged_findings_context_and_peer_review(self):
        rules = os.path.join(self.tmp.name, "aocqc.rules.yaml")
        with open(rules, "w", encoding="utf-8") as h:
            h.write("layers:\n  domain: {paths: [app/**]}\n")
        report = os.path.join(self.tmp.name, "report.json")
        with open(report, "w", encoding="utf-8") as h:
            json.dump({"verdict": "green", "findings": []}, h)
        replies = {
            # Only security sees the unused password, so the lenses disagree and peers rank.
            "lens:security": {"findings": [SQL_FINDING, {"file": "app/db.py", "line": 1, "severity": "low",
                                                         "message": "password is accepted but never used"}]},
            "lens:contrarian": {"findings": [dict(SQL_FINDING, severity="critical", line=3)]},
            "lens": {"findings": []},
            "peer": {"ranking": ["F1", "F2"]},
            "chairman": {"ruling": "reject", "justification": "SQL injection",
                         "action_plan": ["Bind parameters"], "blocking_finding_ids": ["F1"]},
        }
        p = self.run_council(replies, "--format", "json", "--rules", rules, "--report", report, "--no-cache")
        self.assertEqual(p.returncode, 1, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["schema"], "sw-council.ruling/v1")
        self.assertEqual(out["ruling"], "reject")
        self.assertEqual(out["action_plan"], ["Bind parameters"])
        sql = [f for f in out["findings"] if f["message"].startswith("SQL")]
        self.assertEqual(len(sql), 1)
        self.assertEqual(sql[0]["severity"], "critical")
        self.assertEqual(sorted(sql[0]["lenses"]), ["contrarian", "security"])
        self.assertTrue(sql[0]["blocking"])
        self.assertTrue(3 <= len(out["lenses"]) <= 4)
        self.assertIn("security", out["lenses"])
        self.assertEqual(out["models"]["chairman"], "fake:chair")
        self.assertTrue(out["stats"]["peer_review"])
        roles = [r["role"] for r in self.sent()]
        self.assertIn("peer", roles)
        lens_prompt = next(r for r in self.sent() if r["role"] == "lens:security")["messages"][1]["content"]
        self.assertIn("<rules>", lens_prompt)
        self.assertIn("<deterministic-report>", lens_prompt)
        self.assertIn('<file-diff path="app/db.py">', lens_prompt)
        # Small models for lenses, the chairman model only for the ruling.
        self.assertEqual({r["model"] for r in self.sent() if r["role"].startswith("lens")}, {"small-a", "small-b"})
        self.assertEqual({r["model"] for r in self.sent() if r["role"] == "chairman"}, {"chair"})

    def test_text_mode_keeps_the_verdict_line_and_skips_peer_review_when_lenses_agree(self):
        replies = {"lens": {"findings": []},
                   "chairman": {"ruling": "approve_with_conditions", "justification": "fine", "action_plan": []}}
        p = self.run_council(replies, "--no-cache")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip().splitlines()[-1], "COUNCIL_VERDICT: APPROVED")
        self.assertIn("Step 2 has been skipped - the lenses agree", p.stdout)
        self.assertNotIn("peer", [r["role"] for r in self.sent()])

    def test_lens_results_are_cached_by_diff_rules_lens_and_model(self):
        replies = {"lens": {"findings": []}, "chairman": {"ruling": "approve"}}
        first = json.loads(self.run_council(replies, "--format", "json").stdout)
        second = json.loads(self.run_council(replies, "--format", "json").stdout)
        self.assertEqual(first["stats"]["cache_hits"], 0)
        self.assertEqual(second["stats"]["cache_hits"], len(second["lenses"]))
        lens_calls = [r for r in self.sent() if r["role"].startswith("lens")]
        self.assertEqual(len(lens_calls), len(first["lenses"]))

    def test_an_unreadable_ruling_fails_closed(self):
        p = self.run_council({"lens": {"findings": []}, "chairman": "I think it is probably fine"},
                             "--format", "json", "--no-cache")
        self.assertEqual(p.returncode, 2)
        self.assertIsNone(json.loads(p.stdout)["ruling"])

    def test_a_prose_ruling_still_counts(self):
        p = self.run_council({"lens": {"findings": []}, "chairman": "All good.\nCOUNCIL_VERDICT: APPROVED"},
                             "--no-cache")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_no_lens_answering_is_an_error(self):
        p = self.run_council({"lens": "not json", "chairman": {"ruling": "approve"}}, "--format", "json", "--no-cache")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no lens returned a usable review", p.stdout)

    def test_nothing_to_review_approves(self):
        git(self.repo, "checkout", "--", ".")
        p = self.run_council({}, "--format", "json")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["justification"], "No changes to review.")

    def test_a_release_range_and_named_lenses(self):
        git(self.repo, "commit", "-qam", "change")
        replies = {"lens": {"findings": []}, "chairman": {"ruling": "approve"}}
        p = self.run_council(replies, "--format", "json", "--base", "HEAD~1", "--lenses", "dx,outsider", "--no-cache")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["lenses"], ["dx", "outsider"])


if __name__ == "__main__":
    unittest.main()
