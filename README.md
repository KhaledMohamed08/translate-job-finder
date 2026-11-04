# Weam Job Radar 🌍

Every morning it checks **UN and international organizations** plus **remote job boards**, scores each job for a translator (Arabic · English · French · Korean, about 2 years of experience, based in Egypt), and:

- updates a **phone dashboard** (add it to your home screen like an app)
- sends a **free Telegram message** (or email) with the new best matches

It keeps only jobs that are **remote** or **in Egypt**, and drops remote jobs that are only open to residents of other countries.

Everything is free: GitHub runs it daily and hosts the page, and Telegram bots cost nothing.

## Sources

| Source | What it covers |
|---|---|
| UN Careers | UN Secretariat: all departments, consultancies, internships, language jobs |
| ReliefWeb | UN agencies + NGOs worldwide (UNDP, IOM, UNFPA, NRC, IRC…) |
| Impactpool | Jobs from nearly every UN agency, the EU and development banks (WHO, FAO, ILO, UNOPS…) |
| UNICEF, UNDP, UNESCO, UNHCR, WFP, IMF, ICRC, Green Climate Fund, CLEAR Global | Their own career sites |
| TransPerfect, Welocalize, RWS, Keywords Studios | Translation and localization agencies |
| Google Jobs (via JSearch) | Egypt and remote translator jobs from sites that block direct access. Free plan: 100 requests a month, so the tool uses 3 a day, once a day, and stops at 90 a month |
| Himalayas, Remotive, RemoteOK, Working Nomads, Jobicy | Remote jobs (translation, localization, Arabic/French/Korean AI-language work) |
| DeepL, Phrase, Smartling, Lokalise, Duolingo, ElevenLabs, Rev | Language-tech companies' job boards |

## Setup (about 15 minutes, once)

### 1. Put it on GitHub
1. Create a free account at github.com if you don't have one.
2. Click **+ → New repository**, name it `job-radar`, choose **Public**, then **Create**. (Free hosting needs a public repo. It contains only the job list, no personal data.)
3. Click **uploading an existing file** and drag in everything from this folder: `fetch_jobs.py`, `profile.json`, `README.md` and the `docs` folder. Commit.
4. Hidden folders often don't upload by drag-and-drop, so add the schedule by hand: **Add file → Create new file**, name it `.github/workflows/daily.yml`, paste in the contents of that file from this folder, and commit.

### 2. Turn on the website and the daily run
1. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, folder = `/docs`, then **Save**.
2. **Settings → Actions → General → Workflow permissions**: choose **Read and write permissions**, then **Save**.
3. After a minute your dashboard is live at **`https://YOUR-USERNAME.github.io/job-radar/`**

### 3. Telegram alerts (free)
1. In Telegram, open **@BotFather** and send `/newbot`. Pick a name. It replies with a **token** like `123456:ABC...`
2. **Each of you** opens your new bot and taps **Start** (send it any message).
3. In a browser, open `https://api.telegram.org/botYOUR_TOKEN/getUpdates`. Find `"chat":{"id":123456789` for each of you.
4. On GitHub, go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `TELEGRAM_BOT_TOKEN` = the token
   - `TELEGRAM_CHAT_ID` = both ids separated by a comma, e.g. `123456789,987654321`

*(Optional) Email instead of or as well as Telegram: create a Gmail **App password** (Google Account → Security → 2-Step Verification → App passwords), then add the secrets `SMTP_USER` (your Gmail address), `SMTP_PASSWORD` (the app password) and `EMAIL_TO` (one or more addresses, comma-separated).*

### ReliefWeb (recommended, free)
ReliefWeb blocks its public feed for GitHub's servers. Request a free **app name** with ReliefWeb's [short form](https://docs.google.com/forms/d/e/1FAIpQLScR5EE_SBhweLLg_2xMCnXNbT6md4zxqIB00OL0yZWyrqX_Nw/viewform) (details: <https://apidoc.reliefweb.int/#appname>). Suggested name: `translate-job-finder-` plus a few random characters, e.g. `translate-job-finder-k7q2`. ReliefWeb replies by email. Once it's approved, add the secret `RELIEFWEB_APPNAME` with that name.

### 4. First run
**Actions → Daily job search → Run workflow.** After that it runs every day at about 7–8 am Cairo time. Telegram only messages you when there are **new** jobs scoring 55 or higher.

### 5. On your phones
Open the dashboard link, then:
- **iPhone (Safari):** Share → **Add to Home Screen**
- **Android (Chrome):** ⋮ → **Add to Home screen**

## Using the dashboard
- **Score (0–100):** higher means a better fit. Points come from a translation role, Arabic, French or Korean in the posting, being remote or in Egypt, being a UN or international organization, and the level. Senior roles, 7+ years required, or another language pair lower the score.
- **Filters:** New · Translation · UN & Intl · Remote · Egypt · Arabic · French · Korean · Closing soon · My list
- **Track…:** mark a job *Saved → Applied → Interview → Offer*, or *Not for me* to hide it.
- **⇅ Sync:** your tracking is saved on each phone. To copy it to the other phone, tap ⇅ → *Copy code*, send it (e.g. on WhatsApp), and on the other phone tap ⇅ → paste → *Import*.
- The dots at the bottom show whether each source worked today.

## Tuning
Edit `profile.json` on GitHub (click the file, then the ✏️ pencil):
- `min_score`: lower it to see more jobs, raise it to see fewer
- `notify_min_score`: how good a job must be to trigger a Telegram message
- `languages`, `role_keywords`: what counts as a match

Run locally (optional, Python 3.9+, nothing to install): `python3 fetch_jobs.py`, then `python3 -m http.server -d docs` and open http://localhost:8000

## Worth knowing, outside this tool
- **UN Language Professionals exams (LCE / LPE):** the main route into permanent UN translator jobs. Arabic plus English and French is exactly the profile they test. Watch [languagecareers.un.org](https://languagecareers.un.org).
- **UN Volunteers online** ([app.unv.org](https://app.unv.org)): remote translation volunteering for UN agencies. It builds UN references fast.
- Egypt is eligible for the **UN Young Professionals Programme** in some years. Check the country list each year.
