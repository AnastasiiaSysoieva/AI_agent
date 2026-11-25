"""Vacancy sources: LinkedIn/Indeed, remote-job APIs, RSS, company career sites (ATS APIs)."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from common import Job, clean, http, log, make_job, parse_dt, strip_html

PYTHON_RE = re.compile(r"(?<![a-z])(python|django|fastapi|flask)", re.I)
DEFAULT_CUSTOM_KEYWORDS = ["python", "django", "fastapi", "backend", "back-end", "back end"]


def is_python_related(job: Job) -> bool:
    if job.trusted or not job.description:
        return True  # nothing to check against: let the LLM decide
    return bool(PYTHON_RE.search(f"{job.title} {job.description}"))


def _fresh(value, hours: float) -> bool:
    dt = parse_dt(value)
    return dt is None or dt >= datetime.now(timezone.utc) - timedelta(hours=hours)


# =========================================================================== #
# 1. LinkedIn + Indeed (python-jobspy)
# =========================================================================== #
def fetch_jobspy(cfg: dict) -> list[Job]:
    from jobspy import scrape_jobs  # heavy import, only when needed

    # LinkedIn searches a region; Indeed needs a real country name ("worldwide" is rejected).
    # Indeed does not combine hours_old with is_remote, so "remote" goes into the query instead.
    plans = [
        ("linkedin", "", {"location": cfg["jobspy_location"], "is_remote": True,
                          "linkedin_fetch_description": True}),
        ("indeed", " remote", {"location": cfg["jobspy_indeed_location"],
                               "country_indeed": cfg["jobspy_indeed_country"]}),
    ]
    jobs: list[Job] = []
    attempts = failures = 0
    for term in cfg["search_terms"]:
        for site, suffix, extra in plans:
            attempts += 1
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=term + suffix,
                    results_wanted=cfg["jobspy_results_per_term"],
                    hours_old=cfg["hours_old"],
                    verbose=0,
                    **extra,
                )
            except Exception as e:  # noqa: BLE001
                failures += 1
                log.error("jobspy %s failed for '%s': %s", site, term, str(e)[:300])
                continue
            for row in df.to_dict("records"):
                url = clean(row.get("job_url"))
                if url:
                    jobs.append(
                        make_job(
                            url, row.get("title"), row.get("company"),
                            clean(row.get("location")) or "Remote",
                            row.get("description"), clean(row.get("site")).capitalize() or site.capitalize(),
                        )
                    )
            log.info("jobspy %s '%s': %d rows", site, term, len(df))
            time.sleep(2)
    if attempts and failures == attempts:
        raise RuntimeError("all LinkedIn/Indeed requests failed")
    return jobs


# =========================================================================== #
# 2. Remote-job aggregators with open JSON APIs
# =========================================================================== #
def fetch_remotive(cfg: dict) -> list[Job]:
    data = http("GET", "https://remotive.com/api/remote-jobs",
                params={"category": "software-dev", "search": "python"}).json()
    out = []
    for j in data.get("jobs", []):
        if j.get("url") and _fresh(j.get("publication_date"), cfg["hours_old"] * 3):
            out.append(make_job(
                j["url"], j.get("title"), j.get("company_name"),
                "Remote · " + clean(j.get("candidate_required_location")),
                strip_html(j.get("description")), "Remotive"))
    return out


def fetch_arbeitnow(cfg: dict) -> list[Job]:
    data = http("GET", "https://www.arbeitnow.com/api/job-board-api").json()
    out = []
    for j in data.get("data", []):
        if j.get("remote") and j.get("url") and _fresh(j.get("created_at"), cfg["hours_old"] * 3):
            out.append(make_job(
                j["url"], j.get("title"), j.get("company_name"),
                "Remote · " + clean(j.get("location")),
                strip_html(j.get("description")), "Arbeitnow"))
    return out


def fetch_jobicy(cfg: dict) -> list[Job]:
    data = http("GET", "https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": "python"}).json()
    out = []
    for j in data.get("jobs", []):
        if j.get("url") and _fresh(j.get("pubDate"), cfg["hours_old"] * 3):
            out.append(make_job(
                j["url"], j.get("jobTitle"), j.get("companyName"),
                "Remote · " + clean(j.get("jobGeo")),
                strip_html(j.get("jobDescription")), "Jobicy"))
    return out


def fetch_workingnomads(cfg: dict) -> list[Job]:
    data = http("GET", "https://www.workingnomads.com/api/exposed_jobs/").json()
    out = []
    for j in data if isinstance(data, list) else []:
        if j.get("url") and _fresh(j.get("pub_date"), cfg["hours_old"] * 3):
            out.append(make_job(
                j["url"], j.get("title"), j.get("company_name"),
                "Remote · " + clean(j.get("location")),
                strip_html(j.get("description")), "Working Nomads"))
    return out


def fetch_remoteok(cfg: dict) -> list[Job]:
    data = http("GET", "https://remoteok.com/api").json()
    out = []
    for j in data if isinstance(data, list) else []:
        url = j.get("url") if isinstance(j, dict) else None
        if url and j.get("position") and _fresh(j.get("date"), cfg["hours_old"] * 3):
            out.append(make_job(
                url, j.get("position"), j.get("company"),
                "Remote · " + clean(j.get("location")),
                strip_html(j.get("description")), "RemoteOK"))
    return out


AGGREGATORS = {
    "Remotive": fetch_remotive,
    "Arbeitnow": fetch_arbeitnow,
    "Jobicy": fetch_jobicy,
    "Working Nomads": fetch_workingnomads,
    "RemoteOK": fetch_remoteok,
}


# =========================================================================== #
# 3. RSS feeds
# =========================================================================== #
def fetch_rss(cfg: dict) -> list[Job]:
    import feedparser

    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["hours_old"] * 3)
    jobs: list[Job] = []
    for name, url in cfg["rss_feeds"].items():
        try:
            feed = feedparser.parse(http("GET", url).content)
        except Exception as e:  # noqa: BLE001 - one broken feed must not stop the rest
            log.error("RSS %s failed: %s", name, e)
            continue
        count = 0
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            if stamp and datetime(*stamp[:6], tzinfo=timezone.utc) < cutoff:
                continue
            title = strip_html(entry.get("title", ""))
            company = strip_html(entry.get("author", "")) or name
            if name == "WeWorkRemotely" and ": " in title:  # "Company: Job title"
                company, title = title.split(": ", 1)
            jobs.append(make_job(link, title, company, "Remote",
                                 strip_html(entry.get("summary", "")), name))
            count += 1
        log.info("RSS %s: %d fresh entries", name, count)
    return jobs


# =========================================================================== #
# 4. Company career sites
#    Most company sites run on an ATS that has a public JSON API. We use it
#    directly: reliable, fast, no HTML scraping. Unknown sites fall back to a
#    generic link scraper ("custom").
# =========================================================================== #
def _src(c: dict) -> str:
    return f"{c.get('name') or c.get('token')} · сайт компанії"


def _remote(loc, flag) -> str:
    loc = clean(loc)
    return f"Remote · {loc}" if flag and "remote" not in loc.lower() else loc


def _greenhouse(c: dict) -> list[Job]:
    data = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs",
                params={"content": "true"}).json()
    return [
        make_job(j["absolute_url"], j.get("title"), c.get("name"),
                 (j.get("location") or {}).get("name"), strip_html(j.get("content")), _src(c))
        for j in data.get("jobs", []) if j.get("absolute_url")
    ]


def _lever(c: dict) -> list[Job]:
    data = http("GET", f"https://api.lever.co/v0/postings/{c['token']}", params={"mode": "json"}).json()
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = _remote(cats.get("location"), (j.get("workplaceType") or "").lower() == "remote")
        out.append(make_job(
            j.get("hostedUrl"), j.get("text"), c.get("name"), loc,
            j.get("descriptionPlain") or strip_html(j.get("description")), _src(c)))
    return [j for j in out if j.url]


def _ashby(c: dict) -> list[Job]:
    data = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}").json()
    return [
        make_job(j["jobUrl"], j.get("title"), c.get("name"), _remote(j.get("location"), j.get("isRemote")),
                 j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")), _src(c))
        for j in data.get("jobs", []) if j.get("jobUrl")
    ]


def _workable(c: dict) -> list[Job]:
    data = http("GET", f"https://apply.workable.com/api/v1/widget/accounts/{c['token']}",
                params={"details": "true"}).json()
    out = []
    for j in data.get("jobs", []):
        url = j.get("url") or j.get("shortlink")
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        if url:
            out.append(make_job(url, j.get("title"), c.get("name"),
                                _remote(loc, j.get("telecommuting")), strip_html(j.get("description")), _src(c)))
    return out


def _smartrecruiters(c: dict) -> list[Job]:
    data = http("GET", f"https://api.smartrecruiters.com/v1/companies/{c['token']}/postings",
                params={"q": "python", "limit": 100}).json()
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        place = ", ".join(x for x in (loc.get("city"), (loc.get("country") or "").upper()) if x)
        out.append(make_job(
            f"https://jobs.smartrecruiters.com/{c['token']}/{j['id']}", j.get("name"), c.get("name"),
            _remote(place, loc.get("remote")), "Знайдено за запитом 'python' (опис через API недоступний)", _src(c)))
    return out


def _recruitee(c: dict) -> list[Job]:
    data = http("GET", f"https://{c['token']}.recruitee.com/api/offers/").json()
    return [
        make_job(j.get("careers_url"), j.get("title"), c.get("name"),
                 _remote(j.get("location") or j.get("country"), j.get("remote")),
                 strip_html(j.get("description")), _src(c))
        for j in data.get("offers", []) if j.get("careers_url")
    ]


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def _custom(c: dict) -> list[Job]:
    """Fallback for sites without a known ATS: collect links whose text mentions our keywords."""
    keywords = c.get("keywords") or DEFAULT_CUSTOM_KEYWORDS
    rx = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    parser = _LinkParser()
    parser.feed(http("GET", c["url"]).text)
    jobs, seen = [], set()
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        if not 5 <= len(text) <= 160 or not rx.search(text):
            continue
        full = urljoin(c["url"], href)
        if full in seen:
            continue
        seen.add(full)
        jobs.append(make_job(full, text, c.get("name"), "див. сторінку вакансії", text, _src(c), trusted=True))
    if not jobs:
        log.warning("%s: no matching links (the page may be rendered by JavaScript)", c.get("name"))
    return jobs


ATS_HANDLERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
    "smartrecruiters": _smartrecruiters,
    "recruitee": _recruitee,
    "custom": _custom,
}


def fetch_company_raw(c: dict) -> list[Job]:
    handler = ATS_HANDLERS.get(c.get("ats"))
    if handler is None:
        raise ValueError(f"unknown ATS: {c.get('ats')}")
    return handler(c)


def fetch_company(c: dict) -> list[Job]:
    """All Python-related openings of one company."""
    return [j for j in fetch_company_raw(c) if is_python_related(j)]


# --------------------------------------------------------------------------- #
# Auto-detection: "which ATS does this company use?"
# --------------------------------------------------------------------------- #
_ATS_PATTERNS = [
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/(?:v1/boards/)?([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"https?://([A-Za-z0-9-]+)\.recruitee\.com"),
]
_BAD_TOKENS = {"embed", "v1", "boards", "api", "www", "job_board", "jobs", "js"}


def detect_ats(text: str) -> tuple[str, str] | None:
    for ats, pattern in _ATS_PATTERNS:
        for m in re.finditer(pattern, text):
            token = m.group(1)
            if token.lower() not in _BAD_TOKENS:
                return ats, token
    return None


def _pretty(token: str) -> str:
    return re.sub(r"[-_.]+", " ", token).strip().title()


def _domain_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    return _pretty(parts[-2] if len(parts) >= 2 else host)


def resolve_company(arg: str) -> tuple[dict | None, str | None]:
    """
    Turn user input into a company entry. Accepts:
      * https://company.com/careers            (we look for the ATS inside the page)
      * greenhouse:gitlab / lever:spotify ...  (explicit)
      * gitlab                                  (we try every ATS with this name)
    Returns (company, None) or (None, error message).
    """
    arg = arg.strip()
    m = re.fullmatch(r"(greenhouse|lever|ashby|workable|smartrecruiters|recruitee):([\w.%-]+)", arg, re.I)
    if m:
        ats, token = m.group(1).lower(), m.group(2)
        return {"name": _pretty(token), "ats": ats, "token": token}, None

    if arg.lower().startswith(("http://", "https://")):
        found = detect_ats(arg)
        if not found:
            try:
                found = detect_ats(http("GET", arg, retries=1, timeout=20).text)
            except Exception as e:  # noqa: BLE001
                return None, f"не вдалося відкрити сторінку ({type(e).__name__})"
        if found:
            return {"name": _pretty(found[1]), "ats": found[0], "token": found[1]}, None
        return {"name": _domain_name(arg), "ats": "custom", "url": arg}, None

    if re.fullmatch(r"[\w.-]{2,60}", arg):
        slug = arg.lower()
        for ats in ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters"):
            cand = {"name": _pretty(slug), "ats": ats, "token": slug}
            try:
                if fetch_company_raw(cand):
                    return cand, None
            except Exception:  # noqa: BLE001 - 404 means "not on this ATS"
                continue
        return None, "не знайшов цю компанію в жодній з відомих систем найму. Надішліть посилання на сторінку вакансій"
    return None, "не зрозумів запит. Надішліть посилання на сторінку вакансій або формат greenhouse:назва"
