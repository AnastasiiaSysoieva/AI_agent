"""Shared helpers: paths, config/state storage, HTTP, Telegram API, Job model."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
COMPANIES_FILE = ROOT / "companies.json"
SEEN_FILE = ROOT / "seen_jobs.json"
STATE_FILE = ROOT / "state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("job_agent")

esc = html.escape

# --------------------------------------------------------------------------- #
# DEFAULT CONFIG (everything here can be changed in config.json or via the bot)
# --------------------------------------------------------------------------- #
DEFAULT_PROFILE = """
Candidate: Python Backend Developer, Junior / Trainee level, about 1 year of experience.
Stack: Python, Django, Django REST Framework, FastAPI, SQL (PostgreSQL), pytest, Docker, Nginx.
English: B1-B2 (intermediate). Jobs requiring fluent / C1+ English or heavy daily client communication in English are a minus.
Location: lives in Ukraine (Europe/Kyiv timezone). Wants REMOTE work. Jobs restricted to residents of the US/Canada/UK only, office-only jobs and jobs that require relocation = low score.
Experience: up to 1-2 years required is fine; 3+ years of commercial experience required = low score.
"""

DEFAULT_CONFIG: dict = {
    "paused": False,
    "min_score": 7,
    "hours_old": 24,
    "max_llm_per_run": 25,
    "llm_pause_sec": 7,
    "gemini_model": "gemini-2.5-flash",
    "send_summary": False,
    "timezone": "Europe/Kyiv",
    "sources": {"jobspy": True, "aggregators": True, "rss": True, "companies": True},
    "search_terms": [
        "junior python developer",
        "python backend developer junior",
        "django rest framework developer",
        "fastapi developer",
        "trainee python",
    ],
    "jobspy_location": "Europe",
    "jobspy_results_per_term": 15,
    "stop_words": [
        "senior", "sr", "sr.", "lead", "principal", "staff", "head of", "architect",
        "manager", "director", "qa", "tester", "data scientist", "machine learning",
        "ml", "c++", "java", "php", ".net", "asp.net", "golang", "ruby", "frontend",
        "front-end", "react", "devops", "sre", "embedded", "unity",
    ],
    "rss_feeds": {
        "WeWorkRemotely": "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        "Python.org Jobs": "https://www.python.org/jobs/feed/rss/",
        "DOU": "https://jobs.dou.ua/vacancies/feeds/?remote&category=Python",
        "Djinni": "https://djinni.co/jobs/rss/?primary_keyword=Python&employment=remote",
    },
    "profile": DEFAULT_PROFILE.strip(),
}


# --------------------------------------------------------------------------- #
# JSON storage
# --------------------------------------------------------------------------- #
def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.error("Cannot read %s: %s", path.name, e)
        return default


def write_json(path: Path, data, sort_keys: bool = False) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
        encoding="utf-8",
    )


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    stored = read_json(CONFIG_FILE, {})
    if isinstance(stored, dict):
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict) and key != "rss_feeds":
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def load_companies() -> list[dict]:
    data = read_json(COMPANIES_FILE, {"companies": []})
    items = data.get("companies", []) if isinstance(data, dict) else data
    return [c for c in items if isinstance(c, dict) and c.get("ats")]


def save_companies(companies: list[dict]) -> None:
    write_json(COMPANIES_FILE, {"companies": companies})


# --------------------------------------------------------------------------- #
# Job model and text helpers
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str
    trusted: bool = False  # True = already matched by keywords, skip the Python-relevance check


def make_job(url, title, company, location, description, source, trusted=False) -> Job:
    return Job(
        id=make_id(url),
        title=clean(title),
        company=clean(company),
        location=clean(location),
        url=url,
        description=clean(description)[:4000],
        source=source,
        trusted=trusted,
    )


def make_id(url: str) -> str:
    base = url.split("#")[0].split("?")[0].rstrip("/").lower()
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def strip_html(text) -> str:
    text = str(text or "")
    if "&lt;" in text and "<" not in text:  # escaped HTML (Greenhouse)
        text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def clean(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def parse_dt(value):
    """Epoch (s or ms) or ISO string -> aware datetime, or None if unknown."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e11:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(value).strip()
        if s.isdigit():
            return parse_dt(int(s))
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
UA = "Mozilla/5.0 (compatible; job-agent/2.0; +https://github.com)"
_RETRY_STATUS = {429, 500, 502, 503, 504}


def http(method: str, url: str, *, retries: int = 2, timeout: int = 25, **kwargs) -> requests.Response:
    headers = {"User-Agent": UA, **kwargs.pop("headers", {})}
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            if resp.status_code in _RETRY_STATUS and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except requests.HTTPError:
            raise  # 4xx: retrying will not help
        except requests.RequestException as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg(method: str, **payload):
    """Call the Telegram Bot API. Returns `result` or None on error (never logs the token)."""
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        log.error("TG_BOT_TOKEN is not set")
        return None
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.error("Telegram %s failed: %s", method, type(e).__name__)
        return None
    if not data.get("ok"):
        log.error("Telegram %s error: %s", method, data.get("description"))
        return None
    return data.get("result")


def send_message(text: str, reply_markup: dict | None = None):
    payload: dict = {
        "chat_id": os.environ.get("TG_CHAT_ID"),
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", **payload)


def job_keyboard(job_id: str, saved: bool = False, applied: bool = False) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "⭐ Сохранено" if saved else "⭐ Сохранить", "callback_data": f"save:{job_id}"},
                {"text": "✅ Откликнулся" if applied else "✅ Откликнуться", "callback_data": f"applied:{job_id}"},
            ],
            [{"text": "🙈 Скрыть", "callback_data": f"hide:{job_id}"}],
        ]
    }


def fmt_time(iso: str | None, tz_name: str = "Europe/Kyiv") -> str:
    dt = parse_dt(iso)
    if dt is None:
        return "—"
    try:
        from zoneinfo import ZoneInfo

        dt = dt.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - tzdata missing: show UTC
        pass
    return dt.strftime("%d.%m %H:%M")
