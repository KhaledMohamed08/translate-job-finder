"""Tests for fetch_jobs.py. They never touch the network.

Run them with:  python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch_jobs as f  # noqa: E402


def job(title, text="", location="Worldwide", remote=True, intl=False, level="", **kw):
    return f.job("Test", kw.pop("org", "Org"), title, kw.pop("url", "https://example.test/" + title),
                 location=location, text=text, remote=remote, intl=intl, level=level, **kw)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return b"{}"


class Run(unittest.TestCase):
    """Runs main() against fake sources in a temporary folder, capturing messages."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.sent = []
        patches = [
            mock.patch.object(f, "OUT", self.tmp / "jobs.json"),
            mock.patch.object(f, "OUTBOX", self.tmp / "outbox.json"),
            mock.patch.object(f, "check_direct_links", lambda old: ({}, [])),
            mock.patch.object(f.time, "sleep", lambda s: None),
            mock.patch.dict(f.PROFILE, {"notify_min_score": 0}),
            mock.patch.dict(f.JSEARCH, {"month": "", "used": 0, "last_day": ""}),
            mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "111,222",
                                         "DASHBOARD_URL": "https://dash.test/"}, clear=False),
            mock.patch.object(urllib.request, "urlopen", self.fake_urlopen),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DEFER_NOTIFY", None)
        os.environ.pop("ADMIN_CHAT_ID", None)
        self.addCleanup(shutil.rmtree, self.tmp)

    def fake_urlopen(self, req, timeout=0):
        if getattr(req, "data", None):
            self.sent.append(urllib.parse.parse_qs(req.data.decode()))
        return FakeResponse()

    def run_day(self, sources):
        self.sent.clear()
        with mock.patch.object(f, "SOURCES", sources):
            f.main()
        return json.loads(f.OUT.read_text(encoding="utf-8"))

    def texts(self):
        return [m["text"][0] for m in self.sent]


# ---------------------------------------------------------------- scoring

class Scoring(unittest.TestCase):
    def keeps(self, j):
        return f.score(j) is not None

    def test_translation_role_in_her_language_scores_high(self):
        s, reasons, langs = f.score(job("English to Arabic Translator", "Arabic"))
        self.assertGreaterEqual(s, 80)
        self.assertIn("Arabic", langs)

    def test_outside_egypt_must_be_remote(self):
        self.assertFalse(self.keeps(job("Arabic Translator", location="Paris", remote=False)))
        self.assertTrue(self.keeps(job("Arabic Translator", location="Cairo", remote=False)))

    def test_remote_jobs_limited_to_other_countries_are_dropped(self):
        for text in ["Applicants must be located in the United States.",
                     "You must be authorized to work in the US.", "This is a US-based role."]:
            with self.subTest(text=text):
                self.assertFalse(self.keeps(job("French Translator", text, location="Remote (check eligibility)")))

    def test_the_word_us_does_not_trigger_the_country_filter(self):
        self.assertTrue(self.keeps(job("Arabic Translator", "Join us only if you love languages.")))

    def test_general_jobs_outside_egypt_need_language_work(self):
        self.assertFalse(self.keeps(job("IT Risk Consultant", "French required", intl=True)))
        self.assertTrue(self.keeps(job("Communications Consultant", "Fluency in Arabic required", intl=True)))

    def test_unsuitable_and_ai_gig_titles_are_hidden(self):
        for title in ["Driver", "Patent Examiner", "Arabic Language Specialist - AI Trainer", "Sign Language Interpreter"]:
            with self.subTest(title=title):
                self.assertFalse(self.keeps(job(title, "Arabic", location="Cairo", remote=False)))

    def test_other_language_pairs_are_penalised(self):
        s_own = f.score(job("Arabic Translator", "Arabic"))[0]
        result = f.score(job("Chinese Translator", "Arabic", location="Cairo", remote=False))
        self.assertTrue(result is None or result[0] < s_own)

    def test_closed_jobs_are_dropped(self):
        self.assertFalse(self.keeps(job("Arabic Translator", deadline="2000-01-01")))


class Facts(unittest.TestCase):
    def test_pair_years_contract_and_pay(self):
        facts = f.key_facts(job("English to French Translator",
                                "Minimum of 3 years of experience. Pay: $25/hour. Freelance role."))
        self.assertEqual(facts, {"pair": "English → French", "years": 3, "contract": "Freelance", "pay": "$25/hour"})

    def test_word_numbers_and_either_direction(self):
        facts = f.key_facts(job("Korean-English Interpreter", "two (2) years of relevant professional experience"))
        self.assertEqual(facts["pair"], "Korean ↔ English")
        self.assertEqual(facts["years"], 2)

    def test_money_without_a_unit_is_not_pay(self):
        self.assertNotIn("pay", f.key_facts(job("Consultant", "A project worth $5 million.")))


class ApplyEmail(unittest.TestCase):
    def test_only_addresses_given_for_applying(self):
        self.assertEqual(f.apply_email("Please send your CV to jobs@acme.org by Friday."), "jobs@acme.org")
        self.assertEqual(f.apply_email("For privacy questions contact privacy@acme.org."), "")
        self.assertEqual(f.apply_email("Apply: noreply@acme.org"), "")


# ---------------------------------------------------------------- daily run behaviour

class DailyRun(Run):
    def test_new_jobs_are_announced_once(self):
        src = {"A": lambda: [job("Arabic Translator", "Arabic")]}
        self.run_day(src)
        self.assertEqual(len(self.texts()), 2)  # one message per chat
        self.run_day(src)
        self.assertEqual(self.texts(), [])

    def test_alerts_link_to_the_dashboard_job(self):
        self.run_day({"A": lambda: [job("Arabic Translator", "Arabic")]})
        self.assertRegex(self.texts()[0], r'href="https://dash\.test/#job=[a-z0-9]+&amp;u=')

    def test_a_failed_source_keeps_its_jobs_and_does_not_reannounce(self):
        good = {"A": lambda: [job("Arabic Translator", "Arabic")]}

        def broken():
            raise RuntimeError("down")
        self.run_day(good)
        d = self.run_day({"A": broken})
        self.assertEqual(len(d["jobs"]), 1)
        self.run_day(good)
        self.assertEqual(self.texts(), [])

    def test_each_alert_gets_a_new_intro(self):
        intros = []
        for n in range(3):
            self.run_day({"A": lambda n=n: [job(f"Arabic Translator number {n}", "Arabic")]})
            intros.append(self.texts()[0].split("\n")[0])
        self.assertEqual(len(set(intros)), 3)
        self.assertTrue(all("Weam" in i or "وئام" in i for i in intros))

    def test_alerts_wait_for_the_website_when_deferred(self):
        os.environ["DEFER_NOTIFY"] = "1"
        self.addCleanup(os.environ.pop, "DEFER_NOTIFY", None)
        self.run_day({"A": lambda: [job("Arabic Translator", "Arabic")]})
        self.assertEqual(self.texts(), [])
        box = json.loads(f.OUTBOX.read_text(encoding="utf-8"))
        answers = iter(["2000-01-01T00:00:00Z", box["updated"]])
        with mock.patch.object(f, "http_json", lambda *a, **k: {"updated": next(answers)}):
            f.send_outbox()
        self.assertEqual(len(self.texts()), 2)


class ProblemAlerts(Run):
    def failing(self):
        raise RuntimeError("site changed")

    def test_source_failure_is_reported_once_after_two_days(self):
        os.environ["ADMIN_CHAT_ID"] = "999"
        good = lambda: [job("Arabic Translator", "Arabic")]  # noqa: E731
        self.run_day({"A": good, "B": good})
        self.run_day({"A": good, "B": self.failing})
        self.assertFalse(any("job source" in t for t in self.texts()))
        self.run_day({"A": good, "B": self.failing})
        admin = [m for m in self.sent if "job source" in m["text"][0]]
        self.assertEqual(len(admin), 1)
        self.assertEqual(admin[0]["chat_id"][0], "999")
        self.run_day({"A": good, "B": self.failing})
        self.assertFalse(any("job source" in t for t in self.texts()))
        self.run_day({"A": good, "B": good})
        self.assertTrue(any("Working again: B" in t for t in self.texts()))

    def test_no_matches_at_all_is_reported(self):
        self.run_day({"A": lambda: [job("Driver", location="Paris", remote=False)]})
        self.assertTrue(any("no matching jobs" in t for t in self.texts()))
        self.assertEqual(self.sent[-1]["chat_id"][0], "111")  # first chat when there's no admin chat


class LinkChecks(unittest.TestCase):
    def test_broken_link_reported_after_two_days_and_blocked_sites_skipped(self):
        def fake(req, timeout=0):
            if "lionbridge" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO())
            if "proz" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 403, "blocked", {}, io.BytesIO())
            return FakeResponse()
        with mock.patch.object(urllib.request, "urlopen", fake):
            links, broken = f.check_direct_links({})
            self.assertEqual(broken, [])
            self.assertEqual(links["proz"]["state"], "unknown")
            links, broken = f.check_direct_links(links)
            self.assertEqual([p["id"] for p, _ in broken], ["lionbridge"])
            links, broken = f.check_direct_links(links)
            self.assertEqual(broken, [])


class JSearchBudget(unittest.TestCase):
    def test_once_a_day_and_never_over_the_monthly_limit(self):
        calls = []
        fake = lambda url, **kw: calls.append(url) or {"data": []}  # noqa: E731
        with mock.patch.object(f, "http_json", fake), \
             mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "fake"}), \
             mock.patch.dict(f.JSEARCH, {"month": "", "used": 0, "last_day": ""}):
            f.src_jsearch()
            self.assertEqual(len(calls), 3)
            with self.assertRaises(f.FeedSkipped):
                f.src_jsearch()
            f.JSEARCH.update(last_day="", used=f.JSEARCH_MONTHLY_BUDGET - 1)
            with self.assertRaises(f.FeedSkipped):
                f.src_jsearch()
            self.assertEqual(len(calls), 3)


# ---------------------------------------------------------------- project files

class ProjectFiles(unittest.TestCase):
    def test_profile_patterns_compile(self):
        profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
        for key, value in profile.items():
            if isinstance(value, list) and value and isinstance(value[0], str) and key != "other_languages":
                for pattern in value:
                    re.compile(pattern)

    def test_direct_list_is_valid(self):
        groups = json.loads((ROOT / "docs" / "direct.json").read_text(encoding="utf-8"))
        ids = [p["id"] for g in groups for p in g["items"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids")
        for g in groups:
            for p in g["items"]:
                self.assertTrue(p["link"].startswith("https://"), p["id"])
                self.assertTrue(p["name"] and p["what"] and p["tip"], p["id"])

    def test_every_intro_names_weam(self):
        quotes = [q for q in (ROOT / "quotes.txt").read_text(encoding="utf-8").splitlines() if q.strip()]
        self.assertGreaterEqual(len(quotes), 30)
        self.assertTrue(all("Weam" in q or "وئام" in q for q in quotes))

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_dashboard_script_has_no_syntax_errors(self):
        page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(script)
        self.addCleanup(os.unlink, tmp.name)
        result = subprocess.run(["node", "--check", tmp.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
