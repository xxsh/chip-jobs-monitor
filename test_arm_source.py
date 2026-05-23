import json
import unittest

from sources import arm


class ArmSourceTests(unittest.TestCase):
    def test_parse_search_results(self):
        html = """
        <ul id="search-results-jobs" data-results-count="2">
          <li class="job-card fs-start">
            <a class="job-card__title" href="/job/shanghai/a/33099/1" data-job-id="1">Principal, Strategic Partnerships</a>
            <span class="location">Shanghai, China</span>
            <span class="category">Marketing &amp; Communication</span>
          </li>
          <li class="job-card fs-start">
            <a class="job-card__title" href="/job/shenzhen/b/33099/2" data-job-id="2">Director, Smartphone Strategy</a>
            <span class="location">Multiple locations</span>
            <span class="category">Sales</span>
          </li>
        </ul>
        """
        jobs = arm._parse_search_results(html)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "1")
        self.assertEqual(jobs[0]["link"], "https://careers.arm.com/job/shanghai/a/33099/1")
        self.assertEqual(jobs[0]["category"], "Marketing & Communication")
        self.assertEqual(jobs[1]["location"], "Multiple locations")

    def test_normalize_card_reads_job_posting_json_ld(self):
        description = """
        <p><strong>Job Overview:</strong></p>
        <p>Own strategic partnerships in China.</p>
        <p><strong>Key Responsibilities:</strong><br />&bull; Build partner strategy.<br />&bull; Coordinate launches.</p>
        <p><strong>Required Qualifications:</strong><br />&bull; Semiconductor partnerships experience.</p>
        <p><strong>Preferred Qualifications:</strong><br />&bull; AI software ecosystem experience.</p>
        <p><strong>Accommodations at Arm</strong></p><p>Boilerplate.</p>
        """
        posting = {
            "@context": "http://schema.org",
            "@type": "JobPosting",
            "identifier": "2026-17274",
            "title": "Regional Account Manager",
            "datePosted": "2026-4-30",
            "employmentType": "Established",
            "url": "https://careers.arm.com/job/shanghai/regional-account-manager/33099/94549393328",
            "description": description,
            "jobLocation": [
                {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "Shanghai",
                        "addressRegion": "",
                        "addressCountry": "China",
                    },
                }
            ],
        }
        detail_html = f"""
        <html><head>
          <meta name="dimension6" content="Sales">
          <script type="application/ld+json">{json.dumps(posting)}</script>
        </head><body></body></html>
        """
        job = arm._normalize_card(
            {
                "id": "94549393328",
                "title": "Fallback title",
                "link": "https://careers.arm.com/job/shanghai/fallback/33099/94549393328",
                "location": "Shanghai, China",
                "category": "Fallback",
            },
            detail_html,
        )

        self.assertEqual(job["id"], "94549393328")
        self.assertEqual(job["jr"], "2026-17274")
        self.assertEqual(job["name"], "Regional Account Manager")
        self.assertEqual(job["datePosted"], "2026-04-30")
        self.assertEqual(job["locations"], ["Shanghai, China"])
        self.assertEqual(job["department"], "Sales")
        self.assertEqual(job["employmentType"], "Established")
        self.assertEqual(job["summary"], "Own strategic partnerships in China.")
        self.assertEqual(job["responsibilities"], ["Build partner strategy.", "Coordinate launches."])
        self.assertEqual(job["requirements"], ["Semiconductor partnerships experience."])
        self.assertEqual(job["preferred"], ["AI software ecosystem experience."])
        self.assertIn("Own strategic partnerships", job["description"])


if __name__ == "__main__":
    unittest.main()
