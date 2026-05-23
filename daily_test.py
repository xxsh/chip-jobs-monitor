"""Port of daily.test.mjs — pure-function tests for daily.py.

Run: python -m unittest daily_test   (or: python daily_test.py)
"""

import os
import sys
import unittest

import db
import daily


class DailyTests(unittest.TestCase):
    def test_partition_diff(self):
        previous = [{"id": 1, "jr": "JR1", "name": "A"}, {"id": 2, "jr": "JR2", "name": "B"}]
        current = [{"id": 2, "jr": "JR2", "name": "B"}, {"id": 3, "jr": "JR3", "name": "C"}]
        diff = daily.partition_diff(current, previous)
        self.assertEqual([j["jr"] for j in diff["newJobs"]], ["JR3"])
        self.assertEqual(diff["canceledJobs"], [{"jr": "JR1", "title": "A"}])

    def test_job_key_stable_with_id(self):
        self.assertEqual(daily.job_key({"id": 42, "jr": "JR42", "name": "X"}), "42")

    def test_db_resolves_public_job_posted_date(self):
        self.assertEqual(db.to_mysql_datetime("2026-05-20"), "2026-05-20 00:00:00")
        self.assertEqual(
            db.resolve_job_posted_date(
                {"datePosted": "2026-05-20", "postedTs": 1779408000, "creationTs": 1772236800},
                "2026-05-22",
            ),
            "2026-05-20",
        )
        self.assertEqual(db.resolve_job_posted_date({"datePosted": "2026-05-20T01:02:03"}, "2026-05-22"), "2026-05-20")
        self.assertEqual(db.resolve_job_posted_date({"postedTs": 1779408000}, "2026-05-22"), "2026-05-22")
        self.assertEqual(db.resolve_job_posted_date({"creationTs": 1772236800}, "2026-05-22"), "2026-02-28")
        self.assertEqual(db.resolve_job_posted_date({}, "2026-05-22"), "2026-05-22")
        self.assertEqual(db.resolve_job_posted_datetime({"datePosted": "2026-05-20"}), "2026-05-20 00:00:00")

    def test_summarize_fits(self):
        summary = daily.summarize_fits(
            [
                {"suitability": "Strong fit"},
                {"suitability": "Good fit"},
                {"suitability": "Possible stretch"},
                {"suitability": "Possible stretch"},
                {"suitability": "Low fit"},
            ]
        )
        self.assertEqual(summary, {"strongFit": 1, "goodFit": 1, "possibleStretch": 2, "lowFit": 1})

    def test_sort_scored(self):
        sorted_jobs = daily.sort_scored(
            [{"score": 50, "title": "B"}, {"score": 80, "title": "A"}, {"score": 50, "title": "A"}]
        )
        self.assertEqual([f"{j['score']}-{j['title']}" for j in sorted_jobs], ["80-A", "50-A", "50-B"])

    def test_build_report_baseline_no_scoring(self):
        calls = []
        report = daily.build_report(
            current_jobs=[{"id": 1, "jr": "JR1", "name": "A"}],
            previous_jobs=[],
            previous_snapshot_file=None,
            score_fn=lambda jobs: calls.append(1) or [],
        )
        self.assertTrue(report["baselineCreated"])
        self.assertEqual(report["rankedJobCount"], 0)
        self.assertEqual(len(calls), 0)

    def test_build_report_current_snapshot_uses_source_infix(self):
        previous = [{"id": 1, "jr": "JR1", "name": "Old"}]
        current = [*previous, {"id": 2, "jr": "JR2", "name": "New"}]

        arm_report = daily.build_report(
            current_jobs=current,
            previous_jobs=previous,
            previous_snapshot_file="/tmp/2026-05-22_arm_shanghai-china.json",
            report_date="2026-05-23",
            source="arm",
            score_fn=lambda jobs: [],
        )
        nvidia_report = daily.build_report(
            current_jobs=current,
            previous_jobs=previous,
            previous_snapshot_file="/tmp/2026-05-22_shanghai-china.json",
            report_date="2026-05-23",
            source="nvidia",
            score_fn=lambda jobs: [],
        )

        self.assertEqual(arm_report["currentSnapshot"], "2026-05-23_arm_shanghai-china.json")
        self.assertEqual(nvidia_report["currentSnapshot"], "2026-05-23_shanghai-china.json")

    def test_build_report_scores_only_new(self):
        previous = [{"id": 1, "jr": "JR1", "name": "Old"}]
        current = [
            {"id": 1, "jr": "JR1", "name": "Old"},
            {"id": 2, "jr": "JR2", "name": "New role", "link": "https://x", "locations": ["China, Shanghai"]},
        ]
        received = {}

        def fake(jobs):
            received["jobs"] = jobs
            return [
                {
                    "jr": j["jr"], "id": j["id"], "title": j["name"], "link": j.get("link"),
                    "locations": j.get("locations"), "score": 70, "suitability": "Good fit",
                    "recommendation": "Maybe", "matchedReasons": ["m1"], "gapReasons": ["g1"], "verdict": "v",
                }
                for j in jobs
            ]

        report = daily.build_report(
            current_jobs=current, previous_jobs=previous,
            previous_snapshot_file="/tmp/prev.json", score_fn=fake,
        )
        self.assertEqual(len(received["jobs"]), 1)
        self.assertEqual(received["jobs"][0]["jr"], "JR2")
        self.assertEqual(report["addedCount"], 1)
        self.assertEqual(report["rankedJobs"][0]["suitability"], "Good fit")
        self.assertEqual(report["fitSummary"]["goodFit"], 1)

    def test_build_report_skips_already_scored(self):
        day1 = [{"id": 1, "jr": "JR1", "name": "Old"}]
        day2 = [*day1, {"id": 2, "jr": "JR2", "name": "Already scored", "link": "https://x", "locations": ["China, Shanghai"]}]
        received = {}

        def fake(jobs):
            received["jobs"] = jobs
            return []

        report = daily.build_report(
            current_jobs=day2, previous_jobs=day1, previous_snapshot_file="/tmp/2026-05-01.json",
            snapshot_history=[{"label": "2026-05-01", "jobs": day1}, {"label": "2026-05-02", "jobs": day2}],
            successful_score_keys={daily.job_key({"id": 2})}, score_fn=fake,
        )
        self.assertEqual(received["jobs"], [])
        self.assertEqual(report["rankedJobCount"], 0)

    def test_build_report_backfills(self):
        day1 = [{"id": 1, "jr": "JR1", "name": "Old"}]
        day2 = [*day1, {"id": 2, "jr": "JR2", "name": "Missed yesterday", "link": "https://x/2", "locations": ["China, Shanghai"]}]
        day3 = [*day2, {"id": 3, "jr": "JR3", "name": "New today", "link": "https://x/3", "locations": ["China, Shanghai"]}]
        received = {}

        def fake(jobs):
            received["jobs"] = jobs
            return [
                {
                    "jr": j["jr"], "id": j["id"], "title": j["name"], "link": j.get("link"), "locations": j.get("locations"),
                    "score": 80 if j["id"] == 2 else 70, "suitability": "Strong fit" if j["id"] == 2 else "Good fit",
                    "recommendation": "Apply", "matchedReasons": [], "gapReasons": [], "verdict": "Scored.",
                }
                for j in jobs
            ]

        report = daily.build_report(
            current_jobs=day3, previous_jobs=day2, previous_snapshot_file="/tmp/2026-05-02.json",
            snapshot_history=[
                {"label": "2026-05-01", "jobs": day1},
                {"label": "2026-05-02", "jobs": day2},
                {"label": "2026-05-03", "jobs": day3},
            ],
            successful_score_keys=set(), report_date="2026-05-03", score_fn=fake,
        )
        self.assertEqual([f"{j['jr']}:{j['firstSeenDate']}" for j in received["jobs"]], ["JR2:2026-05-02", "JR3:2026-05-03"])
        self.assertEqual(report["addedCount"], 1)
        self.assertEqual(report["backlogCount"], 1)
        self.assertEqual(report["scoredDates"], ["2026-05-02", "2026-05-03"])
        self.assertEqual([f"{j['jr']}:{j['firstSeenDate']}" for j in report["rankedJobs"]], ["JR2:2026-05-02", "JR3:2026-05-03"])

    def test_build_report_defers_excess(self):
        day1 = [{"id": 1, "jr": "JR1", "name": "Old"}]
        day2 = [
            *day1,
            {"id": 2, "jr": "JR2", "name": "Queued A", "link": "https://x/2", "locations": ["China, Shanghai"]},
            {"id": 3, "jr": "JR3", "name": "Queued B", "link": "https://x/3", "locations": ["China, Shanghai"]},
            {"id": 4, "jr": "JR4", "name": "Queued C", "link": "https://x/4", "locations": ["China, Shanghai"]},
        ]
        received = {}

        def fake(jobs):
            received["jobs"] = jobs
            return [
                {
                    "jr": j["jr"], "id": j["id"], "title": j["name"], "link": j.get("link"), "locations": j.get("locations"),
                    "score": 70, "suitability": "Good fit", "recommendation": "Maybe",
                    "matchedReasons": [], "gapReasons": [], "verdict": "Scored.",
                }
                for j in jobs
            ]

        report = daily.build_report(
            current_jobs=day2, previous_jobs=day1, previous_snapshot_file="/tmp/2026-05-01.json",
            snapshot_history=[{"label": "2026-05-01", "jobs": day1}, {"label": "2026-05-02", "jobs": day2}],
            successful_score_keys=set(), report_date="2026-05-02", max_scoring_jobs_per_run=1, score_fn=fake,
        )
        self.assertEqual([j["jr"] for j in received["jobs"]], ["JR2"])
        self.assertEqual(report["rankedJobCount"], 1)
        self.assertEqual(report["deferredScoreCount"], 2)
        self.assertEqual(report["remainingUnscoredCount"], 2)
        self.assertEqual(report["deferredDates"], ["2026-05-02"])
        self.assertRegex(daily.render_telegram_digest(report), r"2 queued")

    def test_collect_successful_score_keys(self):
        keys = daily.collect_successful_score_keys(
            [
                {
                    "report": {
                        "rankedJobs": [
                            {"id": 1, "jr": "JR1", "title": "Good", "score": 72},
                            {"id": 2, "jr": "JR2", "title": "Failed", "score": 0, "error": "401 Unauthorized"},
                        ]
                    }
                }
            ]
        )
        self.assertIn(daily.job_key({"id": 1}), keys)
        self.assertNotIn(daily.job_key({"id": 2}), keys)

    def test_build_report_skips_intern(self):
        previous = [{"id": 1, "jr": "JR1", "name": "Old"}]
        current = [
            {"id": 1, "jr": "JR1", "name": "Old"},
            {"id": 2, "jr": "JR2", "name": "AI Developer Technology Engineer Intern, CUDA - 2026"},
            {"id": 3, "jr": "JR3", "name": "Senior Validation Engineer"},
        ]
        received = {}

        def fake(jobs):
            received["jobs"] = jobs
            return [
                {
                    "jr": j["jr"], "id": j["id"], "title": j["name"], "score": 70, "suitability": "Good fit",
                    "recommendation": "Maybe", "matchedReasons": [], "gapReasons": [], "verdict": "Scored.",
                }
                for j in jobs
            ]

        report = daily.build_report(
            current_jobs=current, previous_jobs=previous, previous_snapshot_file="/tmp/prev.json", score_fn=fake,
        )
        self.assertEqual([j["jr"] for j in received["jobs"]], ["JR3"])
        intern = next(j for j in report["rankedJobs"] if j["jr"] == "JR2")
        self.assertEqual(intern["recommendation"], "Skip")
        self.assertEqual(intern["score"], 0)
        self.assertEqual(intern["skippedReason"], "intern")

    def test_is_intern_job(self):
        self.assertTrue(daily.is_intern_job({"name": "Software Engineering Intern - 2026"}))
        self.assertTrue(daily.is_intern_job({"title": "Summer Internship - Software"}))
        self.assertTrue(daily.is_intern_job({"title": "ASIC 验证实习生"}))
        self.assertFalse(
            daily.is_intern_job(
                {
                    "name": "Manager/Senior Manager, International Finance - China",
                    "employmentType": "Internship",
                    "description": "Oversee the financial and compliance framework.",
                }
            )
        )
        self.assertFalse(daily.is_intern_job({"name": "Senior Validation Engineer"}))

    def test_render_markdown_no_new(self):
        md = daily.render_markdown(
            {
                "date": "2026-04-30", "currentJobCount": 144, "addedCount": 0, "canceledCount": 0,
                "canceledJobs": [], "profileHighlights": ["semiconductor test", "data analytics"],
                "baselineCreated": False, "rankedJobCount": 0,
                "fitSummary": {"strongFit": 0, "goodFit": 0, "possibleStretch": 0, "lowFit": 0}, "rankedJobs": [],
            }
        )
        self.assertRegex(md, r"Jobs today: 144")
        self.assertRegex(md, r"Profile: semiconductor test, data analytics")
        self.assertRegex(md, r"No newly added NVIDIA jobs today")

    def test_render_markdown_ranked(self):
        md = daily.render_markdown(
            {
                "date": "2026-04-30", "currentJobCount": 145, "addedCount": 1, "canceledCount": 0,
                "canceledJobs": [], "profileHighlights": [], "baselineCreated": False, "rankedJobCount": 1,
                "fitSummary": {"strongFit": 0, "goodFit": 1, "possibleStretch": 0, "lowFit": 0},
                "rankedJobs": [
                    {
                        "jr": "JR9", "title": "Test Engineer", "link": "https://x", "score": 72, "suitability": "Good fit",
                        "recommendation": "Apply", "firstSeenDate": "2026-04-29", "posted": "2026-04-30",
                        "locations": ["China, Shanghai"], "verdict": "Looks promising.",
                        "matchedReasons": ["ATE background fits."], "gapReasons": ["No CUDA."],
                    }
                ],
            }
        )
        for pat in [r"\[JR9\] Test Engineer", r"Good fit \(72\)", r"Added: 2026-04-29", r"Verdict: Looks promising", r"ATE background fits", r"No CUDA"]:
            self.assertRegex(md, pat)

    def test_telegram_fits_limit(self):
        many = [
            {
                "jr": f"JR{1000 + i}",
                "title": f"Long-titled Senior Validation Methodology Engineer for Silicon Position {i}",
                "link": f"https://jobs.nvidia.com/careers/job/{1000 + i}", "score": 70 - i,
                "suitability": "Good fit" if i < 3 else "Possible stretch" if i < 15 else "Low fit",
                "recommendation": "Maybe" if i < 15 else "Skip",
                "verdict": "A long-form verdict that explains the fit and gaps in considerable detail. " * 4,
                "matchedReasons": ["m1", "m2"], "gapReasons": ["g1", "g2"],
            }
            for i in range(30)
        ]
        digest = daily.render_telegram_digest(
            {
                "date": "2026-04-26", "location": "Shanghai, China", "currentJobCount": 200, "addedCount": 30,
                "canceledCount": 5, "canceledJobs": [{"jr": f"JR-X{i}", "title": f"removed {i}"} for i in range(5)],
                "profileHighlights": [], "baselineCreated": False,
                "fitSummary": {"strongFit": 0, "goodFit": 3, "possibleStretch": 12, "lowFit": 15}, "rankedJobs": many,
            }
        )
        self.assertLessEqual(len(digest), 4096)
        self.assertRegex(digest, r"NVIDIA Shanghai, China — 2026-04-26")
        self.assertRegex(digest, r"\+30 added")
        self.assertRegex(digest, r"JR1000")

    def test_telegram_no_new_with_cancellations(self):
        digest = daily.render_telegram_digest(
            {
                "date": "2026-04-26", "location": "Shanghai, China", "currentJobCount": 147, "addedCount": 0,
                "canceledCount": 2, "canceledJobs": [{"jr": "JR-X", "title": "removed A"}, {"jr": "JR-Y", "title": "removed B"}],
                "profileHighlights": [], "baselineCreated": False,
                "fitSummary": {"strongFit": 0, "goodFit": 0, "possibleStretch": 0, "lowFit": 0}, "rankedJobs": [],
            }
        )
        self.assertRegex(digest, r"No new jobs today")
        self.assertRegex(digest, r"❌ Canceled \(2\)")
        self.assertRegex(digest, r"removed A")
        self.assertLess(len(digest), 500)

    def test_telegram_backfill_first_seen(self):
        digest = daily.render_telegram_digest(
            {
                "date": "2026-05-03", "location": "Shanghai, China", "currentJobCount": 100, "addedCount": 0,
                "backlogCount": 1, "rankedJobCount": 1, "canceledCount": 0, "canceledJobs": [],
                "profileHighlights": [], "baselineCreated": False,
                "fitSummary": {"strongFit": 1, "goodFit": 0, "possibleStretch": 0, "lowFit": 0},
                "rankedJobs": [
                    {
                        "jr": "JR2", "title": "Missed yesterday", "link": "https://x/2", "score": 80,
                        "suitability": "Strong fit", "recommendation": "Apply", "firstSeenDate": "2026-05-02", "verdict": "Good match.",
                    }
                ],
            }
        )
        self.assertRegex(digest, r"1 scored \(1 backfill\)")
        self.assertRegex(digest, r"added 2026-05-02")

    def test_telegram_baseline(self):
        digest = daily.render_telegram_digest(
            {
                "date": "2026-04-26", "location": "Shanghai, China", "currentJobCount": 147, "addedCount": 0,
                "canceledCount": 0, "canceledJobs": [], "profileHighlights": [], "baselineCreated": True,
                "fitSummary": {"strongFit": 0, "goodFit": 0, "possibleStretch": 0, "lowFit": 0}, "rankedJobs": [],
            }
        )
        self.assertRegex(digest, r"Baseline established")

    def test_run_scorer_merges_and_preserves_order(self):
        # Exercises the in-process scorer seam without calling codex, by stubbing score_job.
        src = os.path.join(os.path.dirname(daily.__file__), "scorer", "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        if not os.path.exists(daily.PROFILE_CACHE):
            self.skipTest("profile cache not present")
        import scorer.score as score_mod

        original = score_mod.score_job
        score_mod.score_job = lambda job, profile, *, timeout=120, attempts=1: {
            "score": 55, "suitability": "Possible stretch", "recommendation": "Maybe",
            "matchedReasons": ["m"], "gapReasons": ["g"], "verdict": "v",
        }
        try:
            jobs = [
                {"id": 1, "jr": "JR1", "name": "Role One", "link": "l1",
                 "locations": ["China, Shanghai"], "department": "D",
                 "datePosted": "2026-05-01T00:00:00", "postedTs": 111},
                {"id": 2, "jr": "JR2", "name": "Role Two", "link": "l2", "postedTs": 222},
            ]
            out = daily.run_scorer(jobs)
        finally:
            score_mod.score_job = original

        self.assertEqual([j["jr"] for j in out], ["JR1", "JR2"])  # original order preserved
        self.assertEqual(out[0]["posted"], "2026-05-01T00:00:00")  # datePosted preferred
        self.assertEqual(out[1]["posted"], 222)  # falls back to postedTs
        self.assertEqual(out[0]["title"], "Role One")
        self.assertEqual(out[0]["suitability"], "Possible stretch")
        self.assertEqual(daily.run_scorer([]), [])

    def _grouped(self):
        nvidia = {
            "date": "2026-05-23", "location": "Shanghai, China", "baselineCreated": False,
            "addedCount": 2, "canceledCount": 1, "rankedJobCount": 2, "backlogCount": 0,
            "deferredScoreCount": 0, "scoreErrorCount": 0, "currentJobCount": 100,
            "canceledJobs": [{"jr": "JRX", "title": "Old role"}],
            "rankedJobs": [
                {"jr": "JR1", "title": "ASIC Eng", "score": 78, "suitability": "Strong fit", "recommendation": "Apply", "link": "https://n/1", "verdict": "Great match.", "firstSeenDate": "2026-05-23"},
                {"jr": "JR2", "title": "Intern role", "score": 0, "suitability": "Low fit", "recommendation": "Skip", "link": "https://n/2", "verdict": "Intern."},
            ],
        }
        amd = {
            "date": "2026-05-23", "location": "Shanghai, China", "baselineCreated": True,
            "addedCount": 0, "canceledCount": 0, "rankedJobCount": 0, "backlogCount": 0,
            "deferredScoreCount": 0, "scoreErrorCount": 0, "currentJobCount": 40,
            "canceledJobs": [], "rankedJobs": [],
        }
        return {
            "date": "2026-05-23", "location": "Shanghai, China",
            "sources": [
                {"source": "nvidia", "display": "NVIDIA", "report": nvidia},
                {"source": "amd", "display": "AMD", "report": amd},
            ],
            "totals": {"currentJobCount": 140, "addedCount": 2, "canceledCount": 1, "rankedJobCount": 2,
                       "backlogCount": 0, "deferredScoreCount": 0, "scoreErrorCount": 0},
        }

    def test_render_grouped_markdown(self):
        md = daily.render_grouped_markdown(self._grouped())
        self.assertRegex(md, r"Daily jobs — Shanghai, China — 2026-05-23")
        self.assertRegex(md, r"## NVIDIA \(\+2, 2 scored, -1\)")
        self.assertRegex(md, r"## AMD \(\+0, 0 scored, -0\)")
        self.assertRegex(md, r"\[JR1\] ASIC Eng")
        self.assertRegex(md, r"Baseline established today")
        self.assertRegex(md, r"\*\*Canceled:\*\* \[JRX\] Old role")

    def test_render_grouped_telegram(self):
        tg = daily.render_grouped_telegram(self._grouped())
        self.assertLessEqual(len(tg), 4096)
        self.assertRegex(tg, r"🦀 Jobs — Shanghai, China — 2026-05-23")
        self.assertRegex(tg, r"=== NVIDIA \(\+2\) ===")
        self.assertRegex(tg, r"=== AMD ===")  # baseline section
        self.assertRegex(tg, r"🟢 \[JR1\] ASIC Eng \(78\) · Apply")
        self.assertRegex(tg, r"⚪️ Skip \(1\): JR2")
        self.assertRegex(tg, r"❌ Canceled \(1\): JRX")

    def test_build_grouped_totals(self):
        grouped = self._grouped()
        # build_grouped sums per-source reports
        rebuilt = daily.build_grouped(grouped["sources"])
        self.assertEqual(rebuilt["totals"]["addedCount"], 2)
        self.assertEqual(rebuilt["totals"]["currentJobCount"], 140)

    def test_render_markdown_canceled_section(self):
        md = daily.render_markdown(
            {
                "date": "2026-04-30", "currentJobCount": 100, "addedCount": 0, "canceledCount": 1,
                "canceledJobs": [{"jr": "JR-X", "title": "Removed role"}], "profileHighlights": [],
                "baselineCreated": False, "rankedJobCount": 0,
                "fitSummary": {"strongFit": 0, "goodFit": 0, "possibleStretch": 0, "lowFit": 0}, "rankedJobs": [],
            }
        )
        self.assertRegex(md, r"## Canceled")
        self.assertRegex(md, r"\[JR-X\] Removed role")


if __name__ == "__main__":
    unittest.main()
