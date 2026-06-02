"""Pure-function tests for fetch.py description parsing.

fetch.py imports Playwright lazily (inside create_context), so the parsing
helpers import without a browser.

Run: python -m unittest fetch_test   (or: python fetch_test.py)
"""

import unittest

import fetch


class ParseDescriptionSectionsTests(unittest.TestCase):
    def test_markdown_prefixed_headers_split_sections(self):
        # Regression for JR2016323: some NVIDIA postings render headers as
        # "## What ...:" with a markdown heading prefix. The old anchored regex
        # missed them, collapsing the whole JD into `summary` with empty lists.
        desc = "\n".join([
            "Intro paragraph about the role.",
            "",
            "## What you'll be doing:",
            "",
            "  * Lead platform strategy.",
            "  * Own end-to-end delivery.",
            "",
            "#",
            "",
            "## What we need to see:",
            "",
            "  * 12+ years infrastructure experience.",
            "  * 5+ years ML infrastructure.",
            "",
            "## Ways to stand out from the crowd:",
            "",
            "  * Open-source contributions.",
        ])
        out = fetch.parse_description_sections(desc)
        self.assertEqual(out["responsibilities"], ["Lead platform strategy.", "Own end-to-end delivery."])
        self.assertEqual(out["requirements"], ["12+ years infrastructure experience.", "5+ years ML infrastructure."])
        self.assertEqual(out["preferred"], ["Open-source contributions."])
        self.assertEqual(out["summary"], "Intro paragraph about the role.")

    def test_lone_separator_lines_do_not_leak_into_sections(self):
        # The "#" / "##" / "---" separators between blocks are layout, not content.
        desc = "\n".join([
            "## What we need to see:",
            "  * Real requirement.",
            "#",
            "---",
            "##",
        ])
        out = fetch.parse_description_sections(desc)
        self.assertEqual(out["requirements"], ["Real requirement."])

    def test_bare_headers_still_split(self):
        # Postings without the markdown prefix must keep working (no regression).
        desc = "\n".join([
            "Intro.",
            "What you'll be doing:",
            "  * Do the thing.",
            "What we need to see:",
            "  * Need the skill.",
        ])
        out = fetch.parse_description_sections(desc)
        self.assertEqual(out["responsibilities"], ["Do the thing."])
        self.assertEqual(out["requirements"], ["Need the skill."])
        self.assertEqual(out["preferred"], [])

    def test_curly_apostrophe_and_case_insensitive_header(self):
        desc = "## What You’ll Be Doing\n  * Task."
        out = fetch.parse_description_sections(desc)
        self.assertEqual(out["responsibilities"], ["Task."])


if __name__ == "__main__":
    unittest.main()
