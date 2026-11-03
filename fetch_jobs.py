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
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
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


WORKDAY = [  # (label, list endpoint, public site base)
    ("UNHCR", "https://unhcr.wd3.myworkdayjobs.com/wday/cxs/unhcr/External",
     "https://unhcr.wd3.myworkdayjobs.com/External"),
    ("WFP", "https://wd3.myworkdaysite.com/wday/cxs/wfp/job_openings",
     "https://wd3.myworkdaysite.com/recruiting/wfp/job_openings"),
]


def src_workday():
    role = re.compile("|".join(PROFILE["role_keywords"]), re.I)
    home = re.compile("|".join(PROFILE["home_country"]), re.I)
    out = []
    for org, api, site in WORKDAY:
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
                           intl=True, check_location=False))
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


PREFILTERED = {"UNHCR + WFP"}  # only returns plausible matches, so 0 can be normal
SOURCES = {
    "UN Careers": src_un_careers, "ReliefWeb": src_reliefweb, "UNICEF": src_unicef,
    "UNHCR + WFP": src_workday, "Himalayas": src_himalayas, "Remotive": src_remotive,
    "RemoteOK": src_remoteok, "Working Nomads": src_workingnomads, "Jobicy": src_jobicy,
    "Company boards": src_company_boards,
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
LONG_EXP = re.compile(r"\b(minimum|at least)\s+(of\s+)?(\w+\s+)?\(?(7|8|9|10|12|15|seven|eight|ten|fifteen)\)?\s+years", re.I)

OK_GEO = rx([r"worldwide", r"anywhere", r"global", r"egypt", r"africa", r"middle east", r"\bmena\b",
             r"\bemea\b", r"international", r"all countries", r"\bremote\b$"])
BLOCK_GEO = rx([r"\bus(a)?\b", r"united states", r"america", r"canada", r"\buk\b", r"united kingdom",
                r"europe", r"\beu\b", r"latam", r"latin", r"apac", r"asia", r"australia", r"india",
                r"germany", r"france", r"spain", r"poland", r"brazil", r"mexico", r"philippines",
                r"\bcet\b", r"\best\b", r"\bpst\b", r"north america"])


UN_LANG_LIST = re.compile(r"arabic|chinese|english|french|russian|spanish", re.I)


def drop_boilerplate(text):
    """Remove sentences that just list the UN official / working languages."""
    keep = []
    for sent in re.split(r"(?<=[.;:])\s+", text):
        if "working language" in sent.lower() or len(set(m.lower() for m in UN_LANG_LIST.findall(sent))) >= 4:
            continue
        keep.append(sent)
    return " ".join(keep)


def score(j):
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
        if not loc or not OK_GEO.search(loc):
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

    # e.g. "Polish Native" / "(Spanish)" in the title, without any of her languages
    other = OTHER_LANGS.search(title)
    if other:
        s -= 20 if any(LANGS[n][0].search(title) for n in LANGS) else 40
        reasons.append(f"Needs {other.group(0).title()}")

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
    if LONG_EXP.search(text):
        s -= 15
        reasons.append("Asks 7+ years")

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
    except Exception:
        pass
    time.sleep(5)
    return fn()


def key(j):
    return re.sub(r"[^a-z0-9]", "", (j["title"] + j["org"][:20]).lower())[:80]


def main():
    previous = {}
    if OUT.exists():
        try:
            previous = {p["id"]: p for p in json.loads(OUT.read_text(encoding="utf-8"))["jobs"]}
        except Exception:
            pass

    raw, status = [], {}
    with cf.ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(run_source, fn): name for name, fn in SOURCES.items()}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                got = f.result()
                raw += got
                status[name] = {"ok": bool(got) or name in PREFILTERED, "count": len(got)}
                if not status[name]["ok"]:
                    status[name]["error"] = "returned no jobs (site may have changed)"
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
        old = previous.get(k)
        jobs[k] = {
            "id": k, "title": j["title"], "org": j["org"] or j["source"], "source": j["source"],
            "url": j["url"], "location": j["location"], "remote": j["remote"],
            "egypt": "Based in Egypt" in reasons or "Open to Egypt" in reasons, "intl": j["intl"],
            "deadline": iso(j["deadline"]), "posted": iso(j["posted"]),
            "score": s, "reasons": reasons, "langs": langs,
            "role": "Translation / language role" in reasons,
            "snippet": j["text"][:420],
            "first_seen": old["first_seen"] if old else iso(NOW),
        }

    ordered = sorted(jobs.values(), key=lambda x: (-x["score"], x["deadline"] or "9"))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": iso(NOW), "sources": status, "jobs": ordered},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    new = [j for j in ordered if j["id"] not in previous]
    print(f"\n{len(raw)} fetched -> {len(ordered)} matches ({len(new)} new) -> {OUT}", file=sys.stderr)
    notify([j for j in new if j["score"] >= PROFILE["notify_min_score"]], len(ordered))


# ---------------------------------------------------------------- notifications (free)

def notify(new, total):
    if not new:
        return
    dash = os.environ.get("DASHBOARD_URL", "")
    top = new[:12]

    token, chats = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chats:
        e = html.escape
        lines = [f"<b>🌍 {len(new)} new job match{'es' if len(new) > 1 else ''}</b>\n"]
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
                print(f"Telegram sent to {chat}", file=sys.stderr)
            except Exception as ex:
                print(f"Telegram failed for {chat}: {ex}", file=sys.stderr)

    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if user and pw and to:
        rows = "".join(
            f"<p><b>{j['score']}</b> &middot; <a href=\"{html.escape(j['url'])}\">{html.escape(j['title'])}</a><br>"
            f"<small>{html.escape(j['org'])} &middot; {'Remote' if j['remote'] else html.escape(j['location'])}"
            f"{' &middot; closes ' + j['deadline'][:10] if j['deadline'] else ''}</small></p>" for j in top)
        if dash:
            rows += f'<p><a href="{html.escape(dash)}">Open the dashboard</a></p>'
        msg = MIMEText(rows, "html", "utf-8")
        msg["Subject"] = f"Job Radar: {len(new)} new matches"
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
