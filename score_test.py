"""Pure-function tests for scorer.score prompt construction.

The scorer package lives under scorer/src; add it to sys.path the same way
daily.py does so this runs from the repo root alongside the other suites.

Run: python -m unittest score_test   (or: python score_test.py)
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "scorer", "src"))

from scorer.score import _enforce_score_caps, _format_list, _job_payload, SCHEMA_PATH  # noqa: E402

SMOKE_FIXTURES = os.path.join(ROOT, "fixtures", "scoring_smoke_jobs.json")
PROFILE_CACHE = os.path.join(ROOT, "scorer", "cache", "profile_latest.json")


class FormatListTests(unittest.TestCase):
    def test_empty_points_to_description_not_none_listed(self):
        for empty in ([], None):
            rendered = _format_list(empty)
            self.assertEqual(rendered, "(see description)")
            self.assertNotIn("none listed", rendered.lower())

    def test_nonempty_renders_bullets(self):
        self.assertEqual(_format_list(["a", "b"]), "- a\n- b")


class JobPayloadTests(unittest.TestCase):
    def test_empty_structured_sections_do_not_claim_none_listed(self):
        # JR2016323-style job: requirements live in the description prose, the
        # structured lists are empty. The prompt must not assert "(none listed)".
        job = {
            "title": "Senior Platform AI Engineer",
            "jr": "JR2016323",
            "description": "What we need to see:\n12+ years production infrastructure experience.",
            "responsibilities": [],
            "requirements": [],
            "preferred": [],
        }
        prompt = _job_payload(job, {"headline": "candidate"})
        self.assertNotIn("(none listed)", prompt)
        self.assertIn("(see description)", prompt)
        self.assertIn("12+ years production infrastructure experience.", prompt)

    def test_present_requirements_render_as_bullets(self):
        job = {
            "title": "X",
            "jr": "JR1",
            "description": "d",
            "requirements": ["12+ years infra", "5+ years ML infra"],
        }
        prompt = _job_payload(job, {})
        self.assertIn("- 12+ years infra", prompt)
        self.assertIn("- 5+ years ML infra", prompt)


class PromptProcedureTests(unittest.TestCase):
    def test_prompt_drives_requirement_first_reasoning(self):
        prompt = _job_payload({"title": "X", "jr": "JR1", "description": "d"}, {})
        # Anchored on schema field names (a stable contract), not prose wording.
        for anchor in ("roleArchetype", "coverageMatrix", "criticalGaps"):
            self.assertIn(anchor, prompt)
        # The general (non-archetype) hard cap must be present.
        self.assertIn("below 80", prompt)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_new_diagnostic_fields_present_and_required(self):
        props = self.schema["properties"]
        for field in ("roleArchetype", "coverageMatrix", "criticalGaps", "confidence", "scoreRationale"):
            self.assertIn(field, props)
            self.assertIn(field, self.schema["required"])

    def test_base_fields_still_required(self):
        for field in ("score", "suitability", "recommendation", "matchedReasons", "gapReasons", "verdict"):
            self.assertIn(field, self.schema["required"])

    def test_all_properties_required_strict_mode(self):
        # codex strict structured output expects every property to be required.
        self.assertEqual(set(self.schema["required"]), set(self.schema["properties"].keys()))
        self.assertFalse(self.schema["additionalProperties"])

    def test_coverage_matrix_item_shape(self):
        item = self.schema["properties"]["coverageMatrix"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), {"requirement", "coverage", "evidence"})
        self.assertEqual(item["properties"]["coverage"]["enum"], ["direct", "adjacent", "missing"])


class EnforceScoreCapsTests(unittest.TestCase):
    """Step 3 deterministic backstop: >=2 missing core requirements cannot be Strong."""

    @staticmethod
    def _result(score, *, missing=0, adjacent=0, direct=0, **extra):
        coverage = (
            [{"requirement": f"m{i}", "coverage": "missing", "evidence": "-"} for i in range(missing)]
            + [{"requirement": f"a{i}", "coverage": "adjacent", "evidence": "-"} for i in range(adjacent)]
            + [{"requirement": f"d{i}", "coverage": "direct", "evidence": "-"} for i in range(direct)]
        )
        result = {"score": score, "suitability": "Strong fit", "recommendation": "Apply", "coverageMatrix": coverage}
        result.update(extra)
        return result

    def test_two_missing_caps_strong_to_good(self):
        out = _enforce_score_caps(self._result(85, missing=2, direct=4))
        self.assertEqual(out["score"], 79)
        self.assertEqual(out["suitability"], "Good fit")
        self.assertEqual(out["recommendation"], "Maybe")
        self.assertTrue(out["scoreCapApplied"])
        self.assertEqual(out["uncappedScore"], 85)

    def test_three_missing_caps_top_score(self):
        self.assertEqual(_enforce_score_caps(self._result(100, missing=3))["score"], 79)

    def test_one_missing_does_not_cap(self):
        out = _enforce_score_caps(self._result(90, missing=1, direct=5))
        self.assertEqual(out["score"], 90)
        self.assertNotIn("scoreCapApplied", out)

    def test_below_80_unchanged_even_with_missing(self):
        # The backstop only enforces the >=2-missing -> below-80 rule; it does not
        # touch a score already below 80 (suitability stays whatever the model set).
        out = _enforce_score_caps(self._result(70, missing=3))
        self.assertEqual(out["score"], 70)
        self.assertNotIn("scoreCapApplied", out)

    def test_no_coverage_matrix_is_safe(self):
        # Synthetic results (skipped intern / error fallback) carry no coverageMatrix.
        out = _enforce_score_caps({"score": 0, "suitability": "Low fit"})
        self.assertEqual(out["score"], 0)
        self.assertNotIn("scoreCapApplied", out)

    def test_does_not_mutate_input(self):
        original = self._result(85, missing=2)
        snapshot = json.loads(json.dumps(original))
        _enforce_score_caps(original)
        self.assertEqual(original, snapshot)


@unittest.skipUnless(
    os.environ.get("RUN_SCORING_SMOKE"),
    "live codex diagnostic — set RUN_SCORING_SMOKE=1 (and strip proxy) to run",
)
class ScoringSmokeTests(unittest.TestCase):
    """Opt-in live scoring of the known problem jobs. Diagnostic, not a CI gate:
    asserts on score bands (with margin), not exact numbers, since codex is
    non-deterministic. Run: RUN_SCORING_SMOKE=1 python -m unittest score_test"""

    @classmethod
    def setUpClass(cls):
        if not (os.path.exists(SMOKE_FIXTURES) and os.path.exists(PROFILE_CACHE)):
            raise unittest.SkipTest("missing smoke fixture or profile cache")
        # codex and the proxy don't mix (the daily pipeline strips these too).
        for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ.pop(var, None)
        from pathlib import Path
        from scorer.score import load_profile, score_job
        cls._score_job = staticmethod(score_job)
        cls._profile = load_profile(Path(PROFILE_CACHE))
        with open(SMOKE_FIXTURES, encoding="utf-8") as fixture_file:
            cls._jobs = {job["jr"]: job for job in json.load(fixture_file)}

    def _score(self, jr):
        cls = type(self)
        result = cls._score_job(cls._jobs[jr], cls._profile, timeout=200)
        self.assertIsNone(result.get("error"), result.get("error"))
        return result

    def test_jr2016323_not_strong(self):
        result = self._score("JR2016323")
        self.assertLess(result["score"], 80, result.get("scoreRationale"))
        self.assertNotEqual(result["suitability"], "Strong fit")

    def test_jr2014734_not_strong(self):
        result = self._score("JR2014734")
        self.assertLess(result["score"], 80, result.get("scoreRationale"))
        self.assertNotEqual(result["suitability"], "Strong fit")

    def test_jr2017301_remains_solid(self):
        # Stable control: must not collapse to a stretch/low score.
        result = self._score("JR2017301")
        self.assertGreaterEqual(result["score"], 60, result.get("scoreRationale"))


if __name__ == "__main__":
    unittest.main()
