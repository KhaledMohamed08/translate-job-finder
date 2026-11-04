# Translate Job Radar 🌍

**Dashboard:** https://khaledmohamed08.github.io/translate-job-finder/

Once a day it checks **UN and international organizations**, **translation companies** and **remote job boards**, scores each job for a translator (Arabic, English, French and Korean, about 2 years of experience, based in Egypt), and:

- updates a **phone-friendly dashboard** you can add to your home screen
- sends a **free Telegram message** (or email) with the new best matches

It keeps only jobs that are **remote** or **in Egypt**, and drops remote jobs it can tell are limited to residents of other countries. Some remote jobs can't be confirmed, so they show a warning to check eligibility.

It runs for free: GitHub runs the search and hosts the page, Telegram bots cost nothing, and JSearch and ReliefWeb are used on their free terms.

## Sources

| Source | What it covers |
|---|---|
| UN Careers | UN Secretariat: departments, consultancies, internships, language jobs |
| Impactpool | Jobs from many UN agencies, the EU and development banks. Also searched by name for OECD, IFRC, GGGI, the African Union and the League of Arab States |
| ReliefWeb | UN agencies and NGOs worldwide. **Needs an app name to work on GitHub** (see below) |
| UNICEF, UNDP, UNESCO, UNHCR, WFP, IMF, ICRC, Green Climate Fund, CLEAR Global | Their own career sites |
| Google Jobs (via JSearch) | Egypt and remote translator jobs, including from sites that block direct access |
| TransPerfect, Welocalize, RWS, Keywords Studios | Translation and localization companies |
| DeepL, Phrase, Smartling, Lokalise, Duolingo, ElevenLabs, Rev | Language-tech companies |
| Himalayas, Remotive, RemoteOK, Working Nomads, Jobicy | Remote job boards |

AI-training gigs are filtered out. Sites that block automated access, such as Wuzzuf, Bayt and ProZ, aren't searched directly, so it's worth setting up their own free job alerts.

## Using the dashboard

- **Lists:** Translation, In Egypt, Remote, All jobs and My list.
- **Narrow down:** combine Arabic, French, Korean, UN & international, Closing this week and New.
- **Match label:** every job shows Strong, Good or Possible match, based on a score out of 100. Tap a job to see why it fits and any warnings.
- **Bookmark:** tap it on a job to save it to My list.
- **Track this job:** in the job details, mark it Saved, Applied, Interview or Offer, or choose *Not for me* to hide it.
- **Settings and sources:** choose light or dark mode, copy your list to another phone, and see which sources are working.

Your saved jobs are stored on each device. To copy them, open **Settings and sources**, tap **Copy code**, send the code to the other phone, paste it there and tap **Import code**.

### Add it to your phone
- **iPhone (Safari):** Share → **Add to Home Screen**
- **Android (Chrome):** ⋮ → **Add to Home screen**

## How the daily run works

The search is scheduled for 05:00 UTC, which is about 7–8 am in Cairo. GitHub can start scheduled runs hours late when it's busy.

- **New jobs:** Telegram messages you only when there are new jobs scoring 55 or higher, so some days will be quiet.
- **No repeats:** each job is remembered for 180 days, so it isn't announced twice if it disappears and comes back.
- **Source problems:** if a source fails, its previous jobs stay on the list until it recovers.
- **JSearch limit:** the free plan allows 100 requests a month. The tool uses 3 a day, once a day, and stops at 90 a month.

To run it now, open **Actions → Daily job search → Run workflow** on GitHub.

## Settings

GitHub secrets are in **Settings → Secrets and variables → Actions**.

| Secret | Used for | Status |
|---|---|---|
| `RAPIDAPI_KEY` | Google Jobs via JSearch | Set |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts | Set |
| `RELIEFWEB_APPNAME` | ReliefWeb on GitHub | Waiting for approval |
| `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Optional email alerts | Not set |

### Change who gets Telegram alerts
Everyone who should get alerts sends the bot a new message, then run this from the project folder and paste the bot token:

```bash
./setup_telegram.sh
```

The script replaces the saved list using recent messages, so include everyone each time. The script is set up for this project's GitHub account.

### ReliefWeb
ReliefWeb's public feed returns nothing to GitHub's servers. Request a free app name with [ReliefWeb's form](https://docs.google.com/forms/d/e/1FAIpQLScR5EE_SBhweLLg_2xMCnXNbT6md4zxqIB00OL0yZWyrqX_Nw/viewform) ([details](https://apidoc.reliefweb.int/#appname)). When it's approved, save that exact name as the `RELIEFWEB_APPNAME` secret.

### Email alerts (optional)
Create a Gmail **App password** (Google Account → Security → 2-Step Verification → App passwords), then add `SMTP_USER` (your Gmail address), `SMTP_PASSWORD` (the app password) and `EMAIL_TO` (one or more addresses, separated by commas).

### Tune the matching
Edit `profile.json`:

- `min_score`: lower it to see more jobs, raise it to see fewer
- `notify_min_score`: how good a job must be to trigger an alert
- `languages`, `role_keywords`: what counts as a match
- `exclude_title_or_company`, `exclude_description`: what gets filtered out

### Run it on your computer (optional)
It needs Python 3.9 or newer and nothing else:

```bash
python3 fetch_jobs.py
python3 -m http.server -d docs
```

Then open http://localhost:8000. Without the secrets above, Google Jobs and alerts are skipped.

## Worth doing outside this tool

- **UN freelance translator rosters:** many UN offices keep lists of approved outside translators. Ask their translation units how to apply.
- **UN language examinations:** the main route into UN language jobs. See the [UN language careers page](https://www.un.org/dgacm/en/content/language-careers).
- **Translation company sign-ups:** agencies keep freelancer databases separate from their job pages.
- **UN Volunteers online** ([app.unv.org](https://app.unv.org)): remote volunteering that can include translation.
- **UN Young Professionals Programme:** the list of eligible countries changes each year, so check it before applying.
