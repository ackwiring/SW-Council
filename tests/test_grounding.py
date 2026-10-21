"""Grounding and tool-dedupe guards (council_core.ground_findings, uphold_ruling), reproducing the
2026-10-21 auto-format release runs: python -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))
import council_core as core  # noqa: E402
import release_fixture as fx  # noqa: E402


def lens(raw, lens_id):
    return core.normalise_findings(raw, lens_id)


class Incident(unittest.TestCase):
    def test_every_finding_from_the_blocked_runs_is_set_aside_with_a_reason(self):
        findings = lens(fx.SECURITY_LENS, "security") + lens(fx.PERFORMANCE_LENS, "performance")
        kept, set_aside = core.ground_findings(findings, fx.RELEASE_DIFF, fx.REPORT)
        self.assertEqual(kept, [])
        self.assertEqual(len(set_aside), len(findings))
        reasons = {(f["file"], f["line"], f["lens"]): f["reason"] for f in set_aside}
        self.assertEqual(reasons[(".env.example", None, "security")], "cites no line or quoted text")
        self.assertIn("claims a hard-coded secret, but no cited line assigns a value",
                      reasons[(".env.example", 31, "security")])
        for line in (16, 17, 18):
            self.assertIn("comment or blank line", reasons[(".env.example", line, "security")])
        # Brand colours stay the tool's: rule brand/off-palette-colour at severity low.
        for key in (("src/index.css", 35, "security"), ("src/App.jsx", 638, "security"),
                    ("src/index.css", 235, "security"), ("src/App.jsx", 638, "performance"),
                    ("src/index.css", 35, "performance")):
            self.assertIn("repeats the tool finding brand/off-palette-colour", reasons[key])
            self.assertIn("(low)", reasons[key])
        for line in (444, 3, 1):
            self.assertIn("deleted in the reviewed range", reasons[(fx.DELETED_PY_PATH, line,
                                                                     "performance" if line == 1 else "security")])
        self.assertIn("server/temp/ is not in the reviewed change", reasons[("server/temp/", 1, "performance")])

    def test_grounded_findings_survive(self):
        diff = fx.RELEASE_DIFF + fx.new_file_diff("server/keys.env", "STRIPE_KEY=sk_live_51Habcdef123456\n")
        findings = lens({"findings": [
            # A different issue on a line the brand tool also flagged is not a duplicate.
            {"file": "src/App.jsx", "line": 637, "severity": "medium",
             "evidence": "fetch('/api/run').catch(() => {});",
             "message": "The run request swallows its error, so a failed run looks like success."},
            {"file": "./server/keys.env", "line": 1, "severity": "critical",
             "message": "A live Stripe secret key is hard-coded."},
        ]}, "contrarian")
        kept, set_aside = core.ground_findings(findings, diff, fx.REPORT)
        self.assertEqual(set_aside, [])
        self.assertEqual([f["file"] for f in kept], ["src/App.jsx", "server/keys.env"])

    def test_quoted_evidence_must_be_in_the_change_and_near_the_line(self):
        f = lambda **kw: lens({"findings": [{"file": "src/App.jsx", "line": 637, "message": "bad", **kw}]}, "dx")
        _, aside = core.ground_findings(f(evidence="eval(userInput)"), fx.RELEASE_DIFF, None)
        self.assertIn("quoted evidence is not in src/App.jsx", aside[0]["reason"])
        _, aside = core.ground_findings(f(evidence="fetch('/api/run')", line=10), fx.RELEASE_DIFF, None)
        self.assertIn("not near src/App.jsx:10", aside[0]["reason"])
        _, aside = core.ground_findings(f(line=900), fx.RELEASE_DIFF, None)
        self.assertIn("not a line the reviewed change shows", aside[0]["reason"])

    def test_deleted_files_are_named_not_shown(self):
        block = core.context_block(fx.RELEASE_DIFF, None, None, 10 ** 6)
        self.assertIn(f'<file-deleted path="{fx.DELETED_PY_PATH}"', block)
        self.assertNotIn("eval(user_input)", block)
        self.assertEqual(core.deleted_files(fx.RELEASE_DIFF), {fx.DELETED_PY_PATH})

    def test_literal_values(self):
        for line in ("API_KEY=sk_live_abc123", 'password: "hunter2hunter2"', "export TOKEN='abcd1234'",
                     'const key = "AKIAABCDEFGHIJKLMNOP";'):
            self.assertTrue(core.assigns_literal_value(line), line)
        for line in ("REFORMER_API_KEY=", "# API_KEY=sk_live_abc123", "API_KEY=${API_KEY}", "TOKEN=<your token>",
                     "key = process.env.API_KEY", 'API_KEY=""', "SECRET=changeme", "",
                     "   node -e \"console.log(require('crypto').randomBytes(32).toString('hex'))\""):
            self.assertFalse(core.assigns_literal_value(line), line)
        self.assertTrue(core.is_secret_claim("Secrets should not be hard-coded."))
        self.assertFalse(core.is_secret_claim("The password parameter is never used"))


class Ruling(unittest.TestCase):
    def merged(self):
        return core.merge_findings([{"file": "a.py", "line": 1, "lens": "security", "severity": "high",
                                     "message": "eval of input", "suggested_fix": None, "evidence": "eval(x)"}])

    def reject(self, ids):
        return {"ruling": "reject", "justification": "j", "action_plan": [], "blocking_finding_ids": ids}

    def test_a_reject_naming_only_set_aside_findings_does_not_stand(self):
        ruling, note = core.uphold_ruling(self.reject(["F10", "F6", "F7"]), [])
        self.assertEqual(ruling["ruling"], "approve_with_conditions")
        self.assertEqual(ruling["blocking_finding_ids"], [])
        self.assertIn("no grounded finding", note)

    def test_a_reject_must_name_a_grounded_finding_or_it_is_unreadable(self):
        ruling, note = core.uphold_ruling(self.reject(["F10"]), self.merged())
        self.assertIsNone(ruling)
        self.assertIn("without naming any grounded finding", note)

    def test_valid_names_stand_and_unknown_ones_are_dropped(self):
        ruling, note = core.uphold_ruling(self.reject(["F1", "F10"]), self.merged())
        self.assertEqual((ruling["ruling"], ruling["blocking_finding_ids"]), ("reject", ["F1"]))
        self.assertIn("F10", note)
        approve = {**self.reject([]), "ruling": "approve_with_conditions"}
        self.assertEqual(core.uphold_ruling(approve, []), (approve, None))

    def test_output_record_carries_set_aside_and_note(self):
        rec = core.output_record(self.reject(["F1"]), self.merged(), ["security"], {}, {},
                                 [{"file": "x", "line": None, "lens": "dx", "severity": "high",
                                   "message": "m", "reason": "cites no line or quoted text"}], "n")
        self.assertEqual(rec["set_aside"][0]["reason"], "cites no line or quoted text")
        self.assertEqual(rec["ruling_note"], "n")
        self.assertEqual(rec["findings"][0]["evidence"], "eval(x)")


if __name__ == "__main__":
    unittest.main()
