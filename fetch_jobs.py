#!/usr/bin/env python3
"""Job Radar: collect UN / international-org / remote jobs, score them for a
translator profile, write docs/jobs.json for the dashboard, and send a free
Telegram (or email) digest of new matches.

Standard library only, so it runs anywhere with Python 3.9+ (no pip install).
"""
import concurrent.futures as cf
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "jobs.json"
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
NOW = datetime.now(timezone.utc)
SEEN_DAYS = 180  # how long a removed job is remembered
UA = "Mozilla/5.0 (JobRadar personal job search; +https://github.com)"


# ---------------------------------------------------------------- helpers

def http(url, data=None, headers=None, timeout=30, retries=2):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def http_json(url, data=None, **kw):
    return json.loads(http(url, data=data, **kw))


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_date(s):
    if not s:
        return None
    if isinstance(s, (int, float)) or (isinstance(s, str) and s.isdigit()):
        return datetime.fromtimestamp(int(s), timezone.utc)
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        return None


def job(source, org, title, url, location="", text="", remote=False, deadline=None,
        posted=None, level="", intl=False, check_location=True, allowed=None):
    return {
        "source": source, "org": (org or "").strip(), "title": strip_html(title),
        "url": url, "location": (location or "").strip(), "text": strip_html(text),
        "remote": remote, "deadline": parse_date(deadline), "posted": parse_date(posted),
        "level": level or "", "intl": intl, "check_location": check_location,
        "allowed": allowed,
    }


REMOTE_RE = re.compile(
    r"home[- ]based|\bremote(ly)?\b(?! (area|location|communit|region|village|district|part))"
    r"|work(ing)? from home|telecommut|teleworking|virtual(ly)? based", re.I)


NOT_REMOTE_RE = re.compile(
    r"\bno(t)? remote|remote work is not|not (be )?(done |performed )?remote|cannot be (done )?remote"
    r"|in[- ]person only|on[- ]site only", re.I)


def looks_remote(*parts):
    s = " ".join(p or "" for p in parts)
    return bool(REMOTE_RE.search(s)) and not NOT_REMOTE_RE.search(s)


# ---------------------------------------------------------------- sources
# Each returns a list of job dicts. A failing source never stops the others.

def src_un_careers():
    """UN Secretariat (careers.un.org) - official JSON used by the site itself."""
    out, page = [], 0
    while page < 10:
        d = http_json(
            "https://careers.un.org/api/public/opening/jo/list/filteredV2/en",
            {"filterConfig": {"keyword": ""},
             "pagination": {"page": page, "itemPerPage": 100, "sortBy": "startDate", "sortDirection": -1}})
        items = d["data"]["list"]
        for j in items:
            stations = ", ".join(s.get("description", "").title() for s in j.get("dutyStation") or [])
            desc = j.get("jobDescription", "")
            m = re.search(r"Work Location</div><div class='jobPostingItemContent'>(.*?)</div>", desc, re.S)
            work_loc = strip_html(m.group(1)) if m else ""
            out.append(job(
                "UN Careers", (j.get("dept") or {}).get("name", "United Nations").title(),
                j.get("postingTitle") or j.get("jobTitle"),
                f"https://careers.un.org/jobSearchDescription/{j['jobId']}?language=en",
                location=work_loc if looks_remote(work_loc) else stations,
                text=desc, remote=looks_remote(work_loc, j.get("jobTitle", "")),
                deadline=j.get("endDate"), posted=j.get("startDate"),
                level=(j.get("jl") or {}).get("name", ""), intl=True, check_location=False))
        if len(items) < 100:
            break
        page += 1
    return out


RELIEFWEB_QUERIES = ["translator OR translation OR interpreter OR linguist", "Arabic", "French",
                     "Korean", "Egypt", "home-based", "remote"]


def src_reliefweb():
    """ReliefWeb (UN OCHA): jobs from UN agencies + NGOs worldwide.

    Uses the official API when RELIEFWEB_APPNAME is set (free, approved by ReliefWeb),
    otherwise the public RSS feed (which can block cloud servers such as GitHub's)."""
    appname = os.environ.get("RELIEFWEB_APPNAME")
    if appname:
        return reliefweb_api(appname)
    queries = RELIEFWEB_QUERIES
    seen, out = set(), []
    for q in queries:
        for page in range(2):
            xml = http("https://reliefweb.int/jobs/rss.xml?" + urllib.parse.urlencode({"search": q, "page": page}))
            for item in xml.split("<item>")[1:]:
                g = lambda tag: (re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S) or [None, ""])[1]
                link = g("link").strip()
                if link in seen:
                    continue
                seen.add(link)
                desc = html.unescape(g("description"))
                country = (re.search(r"Country:\s*([^<]+)", desc) or [None, ""])[1].strip()
                orgs = (re.search(r"Organizations?:\s*([^<]+)", desc) or [None, ""])[1].strip()
                closing = (re.search(r"Closing date:\s*([^<]+)", desc) or [None, ""])[1].strip()
                title = html.unescape(g("title"))
                body = strip_html(desc)
                out.append(job("ReliefWeb", orgs, title, link, location=country, text=body,
                               remote=looks_remote(title, body[:1500]), deadline=closing,
                               posted=g("pubDate"), intl=True, check_location=False))
    return out


def reliefweb_api(appname):
    seen, out = set(), []
    for q in RELIEFWEB_QUERIES:
        d = http_json("https://api.reliefweb.int/v2/jobs?appname=" + urllib.parse.quote(appname), {
            "query": {"value": q}, "limit": 100, "sort": ["date.created:desc"],
            "fields": {"include": ["title", "url", "source.name", "country.name",
                                   "date.closing", "date.created", "body"]}})
        for item in d.get("data", []):
            f = item.get("fields", {})
            url = f.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            body = f.get("body", "")
            out.append(job("ReliefWeb", ", ".join(x.get("name", "") for x in f.get("source", [])),
                           f.get("title", ""), url,
                           location=", ".join(x.get("name", "") for x in f.get("country", [])),
                           text=body, remote=looks_remote(f.get("title", ""), body[:1500]),
                           deadline=(f.get("date") or {}).get("closing"),
                           posted=(f.get("date") or {}).get("created"), intl=True, check_location=False))
    return out


def src_unicef():
    page = http("https://jobs.unicef.org/en-us/listing/?page=1&page-items=400", timeout=60)
    out = []
    for block in page.split('class="list-view--item"')[1:]:
        m = re.search(r'class="job-link" href="([^"]+)">(.*?)</a>', block, re.S)
        if not m:
            continue
        loc = (re.search(r'class="location">(.*?)</span>', block, re.S) or [None, ""])[1]
        dl = (re.search(r'<time datetime="([^"]+)"', block) or [None, ""])[1]
        teaser = (re.search(r'class="row--teaser">(.*?)<p><b>Location', block, re.S) or [None, ""])[1]
        title = html.unescape(m.group(2))
        out.append(job("UNICEF", "UNICEF", title, "https://jobs.unicef.org" + m.group(1),
                       location=strip_html(loc), text=teaser, remote=looks_remote(title),
                       deadline=dl, intl=True, check_location=False))
    return out


WORKDAY = [  # (label, list endpoint, public site base, international organization?)
    ("UNHCR", "https://unhcr.wd3.myworkdayjobs.com/wday/cxs/unhcr/External",
     "https://unhcr.wd3.myworkdayjobs.com/External", True),
    ("WFP", "https://wd3.myworkdaysite.com/wday/cxs/wfp/job_openings",
     "https://wd3.myworkdaysite.com/recruiting/wfp/job_openings", True),
    ("IMF", "https://imf.wd5.myworkdayjobs.com/wday/cxs/imf/IMF",
     "https://imf.wd5.myworkdayjobs.com/IMF", True),
    ("Welocalize", "https://welocalize.wd1.myworkdayjobs.com/wday/cxs/welocalize/Welocalize",
     "https://welocalize.wd1.myworkdayjobs.com/Welocalize", False),
]


def src_workday():
    role = re.compile("|".join(PROFILE["role_keywords"]), re.I)
    home = re.compile("|".join(PROFILE["home_country"]), re.I)
    out = []
    for org, api, site, intl in WORKDAY:
        offset, posts = 0, []
        while offset < 600:
            d = http_json(api + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
            posts += d.get("jobPostings", [])
            offset += 20
            if offset >= d.get("total", 0):
                break
        if not posts:
            raise RuntimeError(f"{org}: no postings returned")
        for p in posts:
            title, loc = p.get("title", ""), p.get("locationsText", "")
            # Only open the full description for plausible matches (keeps it fast and polite).
            if not (role.search(title) or home.search(loc) or looks_remote(title, loc)):
                continue
            text, deadline = "", None
            try:
                info = http_json(api + p["externalPath"]).get("jobPostingInfo", {})
                text = info.get("jobDescription", "")
                loc = info.get("location", loc)
                deadline = info.get("endDate")
            except Exception:
                pass
            out.append(job(org, org, title, site + p["externalPath"], location=loc, text=text,
                           remote=looks_remote(title, loc, strip_html(text)[:2000]),
                           deadline=deadline, level=" ".join(p.get("bulletFields") or []),
                           intl=intl, check_location=not intl))
    return out


def plausible(title, location):
    """Worth opening the full posting? Keeps the number of requests small."""
    return bool(ROLE_TITLE.search(title) or HOME.search(location) or looks_remote(title, location))


def src_impactpool():
    """Impactpool lists jobs from nearly every UN agency, the EU and development banks."""
    cards = {}
    for q in ["translator", "translation", "interpreter", "linguist", "reviser", "editor",
              "localization", "Egypt", "Cairo",
              # Organizations whose own sites block or can't be read by this tool
              "OECD", "IFRC", "GGGI", "African Union", "League of Arab States"]:
        for page in (1, 2, 3):
            body = http("https://www.impactpool.org/search?" + urllib.parse.urlencode({"q": q, "page": page}))
            found = re.findall(r"<div class='job'>(.*?)</a></div>", body, re.S)
            for c in found:
                m = re.search(r'href="(/jobs/\d+)"', c)
                t = re.search(r"type='cardTitle'>(.*?)</h3>", c, re.S)
                bits = [strip_html(b) for b in re.findall(r"type='bodyEmphasis'>(.*?)</div>", c, re.S)]
                if m and t:
                    cards[m.group(1)] = (strip_html(t.group(1)), bits)
            if len(found) < 40:
                break
            time.sleep(0.5)
    if not cards:
        raise RuntimeError("no cards found")
    out = []
    for path, (title, bits) in cards.items():
        org, loc = (bits + ["", ""])[:2]
        if not (HOME.search(loc) or looks_remote(title, loc)):
            continue  # outside Egypt and not remote
        url = "https://www.impactpool.org" + path
        text, deadline = "", None
        try:
            text = strip_html(http(url))
            d = re.search(r"Application deadline:\s*(\w+ \d{1,2}, \d{4})", text)
            deadline = d.group(1) if d else None
            text = text[text.find(title, 200):]
        except Exception:
            pass
        out.append(job("Impactpool", org, title, url, location=loc, text=text,
                       remote=looks_remote(title, loc), deadline=deadline,
                       level=" ".join(bits[2:]), intl=True, check_location=False))
        time.sleep(0.5)
    return out


def oracle_jobs(org, host, site, public):
    api = host + "/hcmRestApi/resources/latest/"
    d = http_json(api + "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
                  f"&finder=findReqs;siteNumber={site},limit=200,sortBy=POSTING_DATES_DESC")
    reqs = d["items"][0]["requisitionList"]
    if not reqs:
        raise RuntimeError("no jobs returned")
    out = []
    for r in reqs:
        title, loc = r.get("Title", ""), r.get("PrimaryLocation") or ""
        if not plausible(title, loc):
            continue
        info = http_json(api + "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                         f"&finder=ById;Id=%22{r['Id']}%22,siteNumber={site}")["items"][0]
        text = info.get("ExternalDescriptionStr", "")
        out.append(job(org, org, title, f"{public}/job/{r['Id']}",
                       location=loc, text=text, remote=looks_remote(title, loc),
                       deadline=info.get("ExternalPostedEndDate"), posted=r.get("PostedDate"),
                       level=info.get("RequisitionType", ""), intl=True, check_location=False))
    return out


def src_undp():
    return oracle_jobs("UNDP", "https://estm.fa.em2.oraclecloud.com", "CX_1",
                       "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1")


def src_successfactors(org, base):
    """Search pages only: robots.txt disallows the RSS feed on these sites."""
    out, seen = [], set()
    for row in range(0, 200, 25):
        body = http(f"{base}/search/?q=&startrow={row}")
        rows = re.split(r'class="data-row|class="job-tile job-id-', body)[1:]
        for r in rows:
            m = re.search(r'href="(/job/[^"]+)"[^>]*>(.*?)</a>', r, re.S)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            title = strip_html(m.group(2))
            place = re.search(r'class="jobLocation[^"]*">(.*?)</span>', r, re.S) \
                or re.search(r'Country\s*</span>\s*<[^>]*>(.*?)<', r, re.S)
            loc = strip_html(place.group(1)) if place else ""
            if not plausible(title, loc):
                continue
            text = strip_html(http(base + m.group(1)))
            d = re.search(r"(?:deadline|closing date)[^:]{0,50}:\s*(\d{2}/\d{2}/\d{4}|\d{1,2} \w+ \d{4})", text, re.I)
            out.append(job(org, org, title, base + m.group(1), location=loc, text=text,
                           remote=looks_remote(title, loc), deadline=d.group(1) if d else None,
                           intl=True, check_location=False))
        if len(rows) < 25:
            break
    if not seen:
        raise RuntimeError("no rows found")
    return out


def src_unesco():
    return src_successfactors("UNESCO", "https://careers.unesco.org")


def src_icrc():
    return src_successfactors("ICRC", "https://careers.icrc.org")


def src_gcf():
    """Green Climate Fund (Incheon): staff jobs and its separate consultant site."""
    return (oracle_jobs("Green Climate Fund", "https://iaayou.fa.ocs.oraclecloud.com", "CX_1001",
                        "https://jobs.greenclimate.fund/en/sites/CX_1001")
            + src_successfactors("Green Climate Fund", "https://jobs.consultant.greenclimate.fund"))


def src_clear_global():
    """CLEAR Global (formerly Translators without Borders)."""
    d = http_json("https://clear.bamboohr.com/careers/list")
    out = []
    for j in d.get("result", []):
        city = (j.get("location") or {}).get("city") or ""
        remote = str(j.get("locationType")) == "1" or looks_remote(j.get("jobOpeningName"), city)
        url = f"https://clear.bamboohr.com/careers/{j['id']}"
        text = ""
        try:
            text = (http_json(url + "/detail").get("result", {}).get("jobOpening") or {}).get("description", "")
        except Exception:
            pass
        out.append(job("CLEAR Global", "CLEAR Global", j.get("jobOpeningName"), url,
                       location=city or ("Remote" if remote else ""), text=text, remote=remote,
                       intl=True, check_location=False))
    return out


SMARTRECRUITERS = ["transperfect", "keywordsstudios"]
LEVER = ["rws"]


def src_translation_agencies():
    out = []
    for c in SMARTRECRUITERS:
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{c}/postings?limit=100")
        for j in d.get("content", []):
            l = j.get("location") or {}
            if "DO NOT APPLY" in j.get("name", ""):
                continue
            out.append(job("Translation agencies", j.get("company", {}).get("name") or c.title(), j.get("name"),
                           f"https://jobs.smartrecruiters.com/{c}/{j['id']}",
                           location=l.get("fullLocation") or l.get("city"),
                           remote=bool(l.get("remote")), posted=j.get("releasedDate")))
    for c in LEVER:
        for j in http_json(f"https://api.lever.co/v0/postings/{c}?mode=json"):
            cat = j.get("categories") or {}
            out.append(job("Translation agencies", c.upper(), j.get("text"), j.get("hostedUrl"),
                           location=cat.get("location"), text=j.get("descriptionPlain", ""),
                           remote=j.get("workplaceType") == "remote", posted=j.get("createdAt", 0) // 1000 or None))
    return out


class FeedSkipped(Exception):
    """Source deliberately not fetched this run: don't retry, keep its previous jobs."""
    def __init__(self, message, ok=True):
        super().__init__(message)
        self.ok = ok


# The free JSearch plan is a hard 100 requests a month, so 3 a day, once a day.
JSEARCH_MONTHLY_BUDGET = 90
JSEARCH = {"month": "", "used": 0, "last_day": ""}  # loaded from / saved to jobs.json


def jsearch_queries():
    day = NOW.timetuple().tm_yday
    remote = ["Arabic", "French", "Korean"][day % 3]
    local = [{"query": "English Arabic translator Cairo"}, {"query": "interpreter Egypt"},
             {"query": "localization Egypt"}][day % 3]
    return [
        {"query": "translator in Egypt", "country": "eg"},
        {**local, "country": "eg"},
        {"query": f"{remote} translator remote", "work_from_home": "true"},
    ]


def src_jsearch():
    """Google's job listings via JSearch: the way to reach Egypt jobs on sites that block us."""
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        raise FeedSkipped("no RAPIDAPI_KEY set")
    today, month = NOW.strftime("%Y-%m-%d"), NOW.strftime("%Y-%m")
    if JSEARCH["month"] != month:
        JSEARCH.update(month=month, used=0)
    if JSEARCH["last_day"] == today:
        raise FeedSkipped("already searched today")
    queries = jsearch_queries()
    if JSEARCH["used"] + len(queries) > JSEARCH_MONTHLY_BUDGET:
        raise FeedSkipped("monthly request budget used")
    out, errors = [], []
    for q in queries:
        url = "https://jsearch.p.rapidapi.com/search-v2?" + urllib.parse.urlencode(
            {**q, "page": 1, "date_posted": "month"})
        JSEARCH["used"] += 1  # failed requests can count against the plan too
        try:
            d = http_json(url, retries=0, timeout=90, headers={"x-rapidapi-key": key,
                                                               "x-rapidapi-host": "jsearch.p.rapidapi.com"})
        except urllib.error.HTTPError as e:
            errors.append(f"{e.code} {e.read()[:150]!r}")
            continue
        except Exception as e:
            errors.append(str(e))
            continue
        results = d.get("data", [])
        if isinstance(results, dict):
            results = results.get("jobs", [])
        print(f"JSearch {q['query']!r}: {len(results)} results", file=sys.stderr)
        if results:
            print("  fields:", sorted(k for k, v in results[0].items() if v not in (None, "", [])), file=sys.stderr)
        for j in results:
            egypt = j.get("job_country") == "EG"
            place = [j.get("job_city"), "Egypt" if egypt else j.get("job_country")]
            location = j.get("job_location") or ", ".join(x for x in place if x)
            location = location.split("•")[0].strip()  # drop Google's "• via LinkedIn" suffix
            if q.get("country") == "eg" and not HOME.search(location):
                location = f"{location}, Egypt".strip(", ")  # the search itself was limited to Egypt
            if location == "Anywhere":
                location = "Remote (check eligibility)"  # Google's "Anywhere" is often one country only
            out.append(job("Google Jobs", j.get("employer_name"), j.get("job_title"),
                           j.get("job_apply_link"), location=location,
                           text=j.get("job_description", ""),
                           remote=bool(j.get("job_is_remote")) or looks_remote(j.get("job_title"), j.get("job_description", "")[:800]),
                           deadline=j.get("job_offer_expiration_datetime_utc"),
                           level=j.get("job_employment_type") or "",
                           posted=j.get("job_posted_at_datetime_utc")))
    if len(errors) == len(queries):
        raise FeedSkipped(f"request failed: {errors[0]}", ok=False)
    JSEARCH["last_day"] = today
    if errors:
        print(f"JSearch: {len(errors)} of {len(queries)} searches failed: {errors[0]}", file=sys.stderr)
    return out


def src_himalayas():
    out, seen = [], set()
    for q in ["translation", "translator", "interpreter", "localization", "arabic", "french", "korean"]:
        for offset in (0, 20, 40):
            d = http_json("https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode({"q": q, "offset": offset}))
            for j in d.get("jobs", []):
                if j["guid"] in seen:
                    continue
                seen.add(j["guid"])
                restr = j.get("locationRestrictions") or []
                out.append(job("Himalayas", j.get("companyName"), j.get("title"), j.get("applicationLink"),
                               location=", ".join(restr) or "Worldwide", text=j.get("description", ""),
                               remote=True, deadline=j.get("expiryDate"), posted=j.get("pubDate"),
                               level=" ".join(j.get("seniority") or []), allowed=restr))
            if len(d.get("jobs", [])) < 20:
                break
    return out


def src_remotive():
    d = http_json("https://remotive.com/api/remote-jobs")
    return [job("Remotive", j.get("company_name"), j.get("title"), j.get("url"),
                location=j.get("candidate_required_location"), text=j.get("description"),
                remote=True, posted=j.get("publication_date")) for j in d.get("jobs", [])]


def src_remoteok():
    d = http_json("https://remoteok.com/api")
    return [job("RemoteOK", j.get("company"), j.get("position"), j.get("url"),
                location=j.get("location") or "Worldwide",
                text=j.get("description", "") + " " + " ".join(j.get("tags") or []),
                remote=True, posted=j.get("date")) for j in d[1:] if isinstance(j, dict)]


def src_workingnomads():
    d = http_json("https://www.workingnomads.com/api/exposed_jobs/")
    return [job("Working Nomads", j.get("company_name"), j.get("title"), j.get("url"),
                location=j.get("location"), text=j.get("description", "") + " " + (j.get("tags") or ""),
                remote=True, posted=j.get("pub_date")) for j in d]


def src_jobicy():
    d = http_json("https://jobicy.com/api/v2/remote-jobs?count=100")
    return [job("Jobicy", j.get("companyName"), j.get("jobTitle"), j.get("url"),
                location=j.get("jobGeo"), text=j.get("jobDescription") or j.get("jobExcerpt"),
                remote=True, posted=j.get("pubDate")) for j in d.get("jobs", [])]


# Language-tech companies with public job boards (add more names here).
GREENHOUSE = ["smartling", "duolingo", "lokalise"]
ASHBY = ["deepl", "phrase", "elevenlabs", "rev"]


def src_company_boards():
    out = []
    for c in GREENHOUSE:
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c}/jobs?content=true")
        for j in d.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            out.append(job("Company boards", c.title(), j.get("title"), j.get("absolute_url"),
                           location=loc, text=html.unescape(j.get("content", "")),
                           remote=looks_remote(loc, j.get("title")), posted=j.get("updated_at")))
    for c in ASHBY:
        d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{c}")
        for j in d.get("jobs", []):
            loc = j.get("location", "")
            out.append(job("Company boards", c.title(), j.get("title"), j.get("jobUrl"),
                           location=loc, text=j.get("descriptionPlain", ""),
                           remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote" or looks_remote(loc),
                           posted=j.get("publishedAt")))
    return out


# These only return plausible matches, so 0 can be normal (they raise if the site itself fails).
PREFILTERED = {"UNHCR, WFP, IMF, Welocalize", "UNDP", "UNESCO", "ICRC", "Green Climate Fund",
               "Impactpool", "Google Jobs"}
SOURCES = {
    "UN Careers": src_un_careers, "ReliefWeb": src_reliefweb, "UNICEF": src_unicef,
    "UNHCR, WFP, IMF, Welocalize": src_workday, "Impactpool": src_impactpool, "UNDP": src_undp,
    "UNESCO": src_unesco, "ICRC": src_icrc, "Green Climate Fund": src_gcf,
    "CLEAR Global": src_clear_global, "Translation agencies": src_translation_agencies, "Himalayas": src_himalayas, "Remotive": src_remotive,
    "RemoteOK": src_remoteok, "Working Nomads": src_workingnomads, "Jobicy": src_jobicy,
    "Company boards": src_company_boards, "Google Jobs": src_jsearch,
}


# ---------------------------------------------------------------- scoring

def rx(patterns):
    return re.compile("|".join(patterns), re.I)


ROLE_TITLE = rx(PROFILE["role_keywords"])
ROLE_TEXT = rx(["translat", "interpret", "locali[sz]", "proofread", "subtitl", "terminolog", "linguist"])
HOME = rx(PROFILE["home_country"])
SENIOR = rx(PROFILE["senior_patterns"])
JUNIOR = rx(PROFILE["junior_patterns"])
LANGS = {name: (rx(cfg["patterns"]), cfg["weight"]) for name, cfg in PROFILE["languages"].items()}
OTHER_LANGS = rx([rf"\b{l}\b" for l in PROFILE["other_languages"]])
ADJACENT = rx(PROFILE["adjacent_roles"])
NOT_SUITABLE = rx(PROFILE["not_suitable_titles"])

# Remote, but limited to people in another country
_PLACE = (r"(?:US|USA|U\.S\.(?:A\.)?|UK|EU|(?i:the united states|united states|the united kingdom|united kingdom"
          r"|canada|europe|australia|new zealand|germany|france|spain|italy|japan|south korea|korea|india|brazil|mexico))")
RESTRICTED = re.compile(
    rf"\b(?:US|USA|UK|EU|(?i:canada|europe|united states))[- ](?i:only|based|residents?|citizens?)\b"
    rf"|(?i:must|need to|required to|have to|should)\s+(?i:be\s+)?(?i:currently\s+)?"
    rf"(?i:live|reside|residing|living|located|based|a resident)[^.;]{{0,30}}\b{_PLACE}"
    rf"|(?i:authori[sz]ed|eligible|legally able|right)\s+to\s+work\s+in\s+(?i:the\s+)?{_PLACE}"
    rf"|(?i:residents?|citizens?)\s+of\s+(?i:the\s+)?{_PLACE}"
    rf"|(?i:anywhere in)\s+(?i:the\s+)?{_PLACE}")
OPEN_WORLD = rx([r"worldwide", r"anywhere in the world", r"from anywhere", r"any country", r"all countries",
                 r"\begypt\b", r"\bmena\b"])

# Key facts shown on the dashboard
_LANG = "|".join(["arabic", "english", "french", "korean"] + PROFILE["other_languages"])
PAIR_DIRECTED = re.compile(rf"\b({_LANG})\b\s*(?:to|into|>|→)\s*\b({_LANG})\b", re.I)
PAIR_EITHER = re.compile(rf"\b({_LANG})\b\s*(?:<>|<->|↔|[-–—/])\s*\b({_LANG})\b", re.I)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "twelve": 12, "fifteen": 15}
YEARS = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)\b(?:\s*\(\d{1,2}\))?"
                   r"\s*\+?\s*(?:(?:-|–|to|or)\s*\d{1,2}\s*)?\+?\s*years?['’]?\s+(?:of\s+)?(?:[\w,/-]+\s+){0,5}?experience", re.I)
CONTRACTS = [("Internship", r"\bintern\b|\binternship"), ("Volunteer", r"\bvolunteer"),
             ("Freelance", r"freelance|independent contractor"), ("Consultancy", r"consultan|individual contractor"),
             ("Part-time", r"part[- ]?time"), ("Full-time", r"full[- ]?time"),
             ("Contract", r"contractor|fixed[- ]term|temporary|short[- ]term")]
_MONEY = r"(?:US\$|\$|€|£|EGP\s?|USD\s?|EUR\s?|GBP\s?)\d[\d,.]*k?"
PAY = re.compile(rf"{_MONEY}(?:\s*(?:-|–|to)\s*{_MONEY})?\s*(?:/|per\s+|an?\s+)\s*(?:hour|hr|month|year|annum|word|minute|min|day)\b", re.I)


def key_facts(j):
    title, text = j["title"], j["text"]
    facts = {}
    for source in (title, text):
        m = PAIR_DIRECTED.search(source)
        e = PAIR_EITHER.search(source) if source is title else None
        if m and m.group(1).lower() != m.group(2).lower():
            facts["pair"] = f"{m.group(1).title()} → {m.group(2).title()}"
            break
        if e and e.group(1).lower() != e.group(2).lower():
            facts["pair"] = f"{e.group(1).title()} ↔ {e.group(2).title()}"
            break
    m = YEARS.search(text)
    if m:
        n = WORDS.get(m.group(1).lower()) or int(m.group(1))
        if 1 <= n <= 25:
            facts["years"] = n
    head = f"{title} {j['level']}"
    for name, pattern in CONTRACTS:
        if re.search(pattern, head, re.I):
            facts["contract"] = name
            break
    else:
        for name, pattern in CONTRACTS:
            if re.search(pattern, text[:1200], re.I):
                facts["contract"] = name
                break
    m = PAY.search(f"{title} {text}")
    if m:
        facts["pay"] = re.sub(r"\s+", " ", m.group(0))[:32]
    return facts

OK_GEO = rx([r"worldwide", r"anywhere", r"global", r"egypt", r"africa", r"middle east", r"\bmena\b",
             r"\bemea\b", r"international", r"all countries", r"\bremote\b$"])
BLOCK_GEO = rx([r"\bus(a)?\b", r"united states", r"america", r"canada", r"\buk\b", r"united kingdom",
                r"europe", r"\beu\b", r"latam", r"latin", r"apac", r"asia", r"australia", r"india",
                r"germany", r"france", r"spain", r"poland", r"brazil", r"mexico", r"philippines",
                r"\bcet\b", r"\best\b", r"\bpst\b", r"north america"])


EXCLUDE_NAME = rx(PROFILE["exclude_title_or_company"]) if PROFILE.get("exclude_title_or_company") else None
EXCLUDE_TEXT = rx(PROFILE["exclude_description"]) if PROFILE.get("exclude_description") else None
UN_LANG_LIST = re.compile(r"arabic|chinese|english|french|russian|spanish", re.I)


def drop_boilerplate(text):
    """Remove sentences that just list the UN official / working languages."""
    keep = []
    for sent in re.split(r"(?<=[.;:])\s+", text):
        if "working language" in sent.lower() or len(set(m.lower() for m in UN_LANG_LIST.findall(sent))) >= 4:
            continue
        keep.append(sent)
    return " ".join(keep)


def is_ai_gig(j):
    if EXCLUDE_NAME and EXCLUDE_NAME.search(f"{j['title']} {j['org']}"):
        return True
    # UN and NGO postings can discuss AI as a topic, so only check commercial boards.
    return bool(EXCLUDE_TEXT and not j["intl"] and EXCLUDE_TEXT.search(j["text"]))


def score(j):
    if is_ai_gig(j) or NOT_SUITABLE.search(j["title"]):
        return None
    title, text = j["title"], drop_boilerplate(j["text"])
    full = f"{title} {text}"
    s, reasons, langs = 0, [], []

    in_home = bool(HOME.search(j["location"]))
    if j["allowed"] and not any(HOME.search(a) for a in j["allowed"]):
        return None  # remote, but only for residents of listed countries
    if not (j["remote"] or in_home):
        return None  # outside Egypt it must be remote
    if j["remote"] and not in_home and j["check_location"]:
        loc = j["location"] or ""
        if loc and not OK_GEO.search(loc) and BLOCK_GEO.search(loc):
            return None  # remote, but only for people in e.g. the US or Europe
        if RESTRICTED.search(full):
            return None  # the posting itself says e.g. "US residents only"
        if (not loc or not OK_GEO.search(loc)) and not OPEN_WORLD.search(full):
            reasons.append("Check country eligibility")
    if j["deadline"] and j["deadline"] < NOW - timedelta(hours=12):
        return None
    if j["posted"] and not j["deadline"] and j["posted"] < NOW - timedelta(days=PROFILE["max_age_days"]):
        return None

    role = bool(ROLE_TITLE.search(title))
    if role:
        s += 40
        reasons.insert(0, "Translation / language role")
    elif ROLE_TEXT.search(text):
        s += 12
        reasons.insert(0, "Mentions translation")

    for name, (pat, w) in LANGS.items():
        if pat.search(full):
            s += w + (10 if pat.search(title) else 0)
            langs.append(name)
    if role and langs:
        s += 10
    # Outside Egypt, a general job must be close to language work and use her languages.
    if not role and not in_home and not (langs and ADJACENT.search(title)):
        return None

    # e.g. "Polish Native" / "(Spanish)" in the title, without any of her languages
    other = OTHER_LANGS.search(title)
    if other:
        s -= 20 if any(LANGS[n][0].search(title) for n in LANGS) else 40
        reasons.append(f"Needs {other.group(0).title()}")

    # A language job that names only other languages, e.g. "from Burmese into English"
    other_text = OTHER_LANGS.search(text)
    if role and not langs and not other and other_text:
        s -= 30
        reasons.append(f"Needs {other_text.group(0).title()}")

    if in_home and not j["remote"]:
        s += 20
        reasons.append("Based in Egypt")
    elif in_home:
        s += 10
        reasons.append("Open to Egypt")
    if j["remote"]:
        s += 15
        reasons.append("Remote / home-based")
    if j["intl"]:
        s += 10
    if SENIOR.search(title) or SENIOR.search(j["level"]):
        s -= 25
        reasons.append("Senior level")
    elif JUNIOR.search(title) or JUNIOR.search(j["level"]):
        s += 5
    years = key_facts(j).get("years", 0)
    if years >= 7:
        s -= 15
        reasons.append("Asks 7+ years")
    elif years >= 5:
        s -= 8
        reasons.append("Asks 5+ years")
    if re.search(r"\bintern\b|\binternship", title, re.I):
        reasons.append("Internships are usually for students")

    if s < PROFILE["min_score"]:
        return None
    return max(0, min(100, s)), reasons, langs


# ---------------------------------------------------------------- pipeline

def run_source(fn):
    """One retry, because big pages sometimes time out or come back empty."""
    try:
        got = fn()
        if got:
            return got
    except FeedSkipped:
        raise
    except Exception:
        pass
    time.sleep(5)
    return fn()


def key(j):
    # The same UN job can appear under different org spellings on different sites.
    title = re.sub(r"[^a-z0-9]", "", j["title"].lower())
    return title[:80] if j["intl"] and len(title) >= 25 else (title + re.sub(r"[^a-z0-9]", "", j["org"].lower())[:20])[:80]


def main():
    previous, seen, quote_index = {}, {}, 0
    if OUT.exists():
        try:
            old_data = json.loads(OUT.read_text(encoding="utf-8"))
            previous = {p["id"]: p for p in old_data["jobs"]}
            seen = old_data.get("seen", {})
            JSEARCH.update(old_data.get("jsearch", {}))
            quote_index = old_data.get("quote_index", 0)
        except Exception:
            pass
    # Every job ever listed -> when it was first seen, so a job that vanishes for a
    # while (site outage, temporary delisting) is not announced as new when it returns.
    seen.update({i: p["first_seen"] for i, p in previous.items() if i not in seen})

    raw, status = [], {}
    with cf.ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(run_source, fn): name for name, fn in SOURCES.items()}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                got = f.result()
                for j in got:
                    j["feed"] = name
                raw += got
                status[name] = {"ok": bool(got) or name in PREFILTERED, "count": len(got)}
                if not status[name]["ok"]:
                    status[name]["error"] = "returned no jobs (site may have changed)"
            except FeedSkipped as e:
                status[name] = {"ok": e.ok, "count": 0, "kept": True, "error": str(e)[:200]}
            except Exception as e:
                status[name] = {"ok": False, "error": str(e)[:200]}
            print(f"{name:16} {status[name]}", file=sys.stderr)

    jobs = {}
    for j in raw:
        if not j["url"] or not j["title"]:
            continue
        r = score(j)
        if not r:
            continue
        s, reasons, langs = r
        k = key(j)
        if k in jobs and jobs[k]["score"] >= s:
            continue
        jobs[k] = {
            "id": k, "title": j["title"], "org": j["org"] or j["source"], "source": j["source"],
            "feed": j["feed"],
            "url": j["url"], "location": j["location"], "remote": j["remote"],
            "egypt": "Based in Egypt" in reasons or "Open to Egypt" in reasons, "intl": j["intl"],
            "deadline": iso(j["deadline"]), "posted": iso(j["posted"]),
            "score": s, "reasons": reasons, "langs": langs,
            "role": "Translation / language role" in reasons,
            "facts": key_facts(j),
            "snippet": j["text"][:420],
            "first_seen": seen.get(k, iso(NOW)),
        }

    # A source that failed today keeps yesterday's jobs instead of emptying the list.
    failed = {name for name, st in status.items() if not st["ok"] or st.get("kept")}
    for k, p in previous.items():
        if k not in jobs and p.get("feed", p["source"]) in failed and (p["deadline"] or "9") >= iso(NOW):
            jobs[k] = p

    ordered = sorted(jobs.values(), key=lambda x: (-x["score"], x["deadline"] or "9"))
    OUT.parent.mkdir(exist_ok=True)
    new = [j for j in ordered if j["id"] not in seen]
    seen.update({j["id"]: j["first_seen"] for j in ordered})
    cutoff = iso(NOW - timedelta(days=SEEN_DAYS))
    seen = {i: t for i, t in seen.items() if t >= cutoff or i in jobs}
    alerts = [j for j in new if j["score"] >= PROFILE["notify_min_score"]]
    quote = next_quote(quote_index) if alerts else ""
    OUT.write_text(json.dumps({"updated": iso(NOW), "sources": status, "jobs": ordered, "seen": seen,
                               "jsearch": JSEARCH, "quote_index": quote_index + bool(alerts)},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(raw)} fetched -> {len(ordered)} matches ({len(new)} new) -> {OUT}", file=sys.stderr)
    notify(alerts, len(ordered), quote)


# ---------------------------------------------------------------- notifications (free)

def next_quote(index):
    """The next intro from quotes.txt. The position is saved, so none repeats until all have been used."""
    path = ROOT / "quotes.txt"
    quotes = [q.strip() for q in path.read_text(encoding="utf-8").splitlines() if q.strip()] if path.exists() else []
    return quotes[index % len(quotes)] if quotes else ""


def notify(new, total, quote=""):
    if not new:
        return
    dash = os.environ.get("DASHBOARD_URL", "")
    top = new[:12]

    token, chats = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chats:
        e = html.escape
        lines = [f"<i>{e(quote)}</i>\n"] if quote else []
        lines.append(f"<b>🌍 {len(new)} new job match{'es' if len(new) > 1 else ''}</b>\n")
        for j in top:
            dl = f" · ⏳ {j['deadline'][:10]}" if j["deadline"] else ""
            where = "🏠 Remote" if j["remote"] else "📍 " + e(j["location"][:40])
            lines.append(f"<b>{j['score']}</b> · <a href=\"{e(j['url'])}\">{e(j['title'][:110])}</a>\n"
                         f"   {e(j['org'][:60])} · {where}{dl}")
        if dash:
            lines.append(f"\n📱 <a href=\"{e(dash)}\">Open dashboard</a> ({total} open matches)")
        for chat in [c.strip() for c in chats.split(",") if c.strip()]:
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines),
                                                 "parse_mode": "HTML",
                                                 "disable_web_page_preview": "true"}).encode())
                urllib.request.urlopen(req, timeout=20).read()
                print("Telegram message sent", file=sys.stderr)
            except Exception as ex:
                print(f"Telegram failed: {type(ex).__name__}", file=sys.stderr)

    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if user and pw and to:
        rows = "".join(
            f"<p><b>{j['score']}</b> &middot; <a href=\"{html.escape(j['url'])}\">{html.escape(j['title'])}</a><br>"
            f"<small>{html.escape(j['org'])} &middot; {'Remote' if j['remote'] else html.escape(j['location'])}"
            f"{' &middot; closes ' + j['deadline'][:10] if j['deadline'] else ''}</small></p>" for j in top)
        if quote:
            rows = f"<p><i>{html.escape(quote)}</i></p>" + rows
        if dash:
            rows += f'<p><a href="{html.escape(dash)}">Open the dashboard</a></p>'
        msg = MIMEText(rows, "html", "utf-8")
        msg["Subject"] = f"Weam Job Radar: {len(new)} new matches"
        msg["From"], msg["To"] = user, to
        try:
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, timeout=30) as smtp:
                smtp.login(user, pw)
                smtp.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
            print("Email sent", file=sys.stderr)
        except Exception as ex:
            print(f"Email failed: {ex}", file=sys.stderr)


if __name__ == "__main__":
    main()
