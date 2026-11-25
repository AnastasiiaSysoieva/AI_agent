"""Job-search agent: collect -> filter -> Gemini scoring -> Telegram."""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

import sources
from common import (
    SEEN_FILE, STATE_FILE, Job, esc, job_keyboard, load_companies, load_config, log,
    read_json, send_message, utcnow_iso, write_json,
)

SEEN_TTL_DAYS = 45
SENT_MEMORY = 300  # how many sent vacancies the bot remembers (for the buttons)
JUNIOR_RE = re.compile(r"junior|jr\b|trainee|intern|entry|graduate|джун|джуніор|стаж", re.I)
STACK_RE = re.compile(r"python|django|fastapi|flask|backend|back-end", re.I)
REMOTE_RE = re.compile(r"remote|worldwide|anywhere|europe|emea|ukrain|\beu\b|distributed", re.I)
MODEL_STATE = {"i": 0}  # index of the Gemini model that currently works


def rank(job: Job) -> tuple[int, int, int]:
    """Smaller = evaluated first (the per-run LLM budget is limited)."""
    return (
        0 if STACK_RE.search(job.title) else 1,
        0 if REMOTE_RE.search(f"{job.location} {job.title}") else 1,
        0 if JUNIOR_RE.search(job.title) else 1,
    )


class Evaluation(BaseModel):
    score: int = Field(ge=1, le=10)
    reason: str
    matched_stack: list[str]


# --------------------------------------------------------------------------- #
# filtering
# --------------------------------------------------------------------------- #
def compile_stop_words(words: list[str]):
    parts = [r"(?<!\w)" + re.escape(w.strip()) + r"(?!\w)" for w in words if w.strip()]
    return re.compile("|".join(parts), re.I) if parts else None


# --------------------------------------------------------------------------- #
# collecting
# --------------------------------------------------------------------------- #
def collect(cfg: dict) -> tuple[list[Job], dict[str, int], list[str]]:
    jobs: list[Job] = []
    counts: dict[str, int] = {}
    errors: list[str] = []

    def run(group: str, label: str, fn) -> None:
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - a failing source never stops the others
            log.error("Source %s failed: %s", label, e)
            errors.append(label)
            return
        counts[group] = counts.get(group, 0) + len(result)
        jobs.extend(result)

    on = cfg["sources"]
    if on.get("jobspy"):
        run("LinkedIn/Indeed", "jobspy", lambda: sources.fetch_jobspy(cfg))
    if on.get("aggregators"):
        for name, fn in sources.AGGREGATORS.items():
            run("Remote-агрегатори", name, lambda fn=fn: fn(cfg))
    if on.get("rss"):
        run("RSS", "rss", lambda: sources.fetch_rss(cfg))
    if on.get("companies"):
        for company in load_companies():
            run("Сайти компаній", str(company.get("name")), lambda c=company: sources.fetch_company(c))
            time.sleep(0.5)
    for group, n in counts.items():
        log.info("%s: %d vacancies", group, n)
    return jobs, counts, errors


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
def make_client():
    from google import genai

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def evaluate(client, cfg: dict, job: Job) -> Evaluation | None:
    from google.genai import types

    prompt = f"""You are a strict recruiter-assistant. Rate how well this vacancy fits the candidate.

CANDIDATE PROFILE:
{cfg['profile']}

SCORING RULES:
- 9-10: junior/trainee/up to 1-2 years, Python backend (Django/DRF/FastAPI), remote and available from Ukraine/Europe, English not above B2.
- 7-8: good fit with minor gaps.
- 4-6: partial fit (wrong stack emphasis, unclear level, unclear remote).
- 1-3: senior / 3+ years, office or relocation, remote only for other countries, or C1+ English required.
Write "reason" in Ukrainian, 1-2 short sentences. "matched_stack" = candidate technologies that appear in the vacancy.

VACANCY:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description}
"""
    models = cfg["gemini_models"]
    rate_hits = 0
    for _ in range(3 + len(models)):
        model = models[min(MODEL_STATE["i"], len(models) - 1)]
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=Evaluation,
                    temperature=0.2,
                ),
            )
            return Evaluation.model_validate_json(resp.text)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if (msg.startswith("404") or "NOT_FOUND" in msg) and MODEL_STATE["i"] < len(models) - 1:
                MODEL_STATE["i"] += 1
                log.warning("Gemini model %s is unavailable, switching to %s", model, models[MODEL_STATE["i"]])
                continue
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                rate_hits += 1
                if rate_hits > 3:
                    break
                wait = 30 * rate_hits
                log.warning("Gemini rate limit (429), sleeping %ss", wait)
                time.sleep(wait)
                continue
            log.error("Gemini error for '%s' (model %s): %s", job.title, model, msg[:300])
            return None
    return None


# --------------------------------------------------------------------------- #
# Telegram message
# --------------------------------------------------------------------------- #
def job_message(job: Job, ev: Evaluation) -> str:
    return (
        f"💼 <b>{esc(job.title)}</b>\n"
        f"🏢 {esc(job.company or '—')}\n"
        f"📍 {esc(job.location or '—')}  ·  {esc(job.source)}\n"
        f"⭐ Оцінка ШІ: <b>{ev.score}/10</b>\n"
        f"🛠 {esc(', '.join(ev.matched_stack) or '—')}\n\n"
        f"💬 {esc(ev.reason)}\n\n"
        f'🔗 <a href="{esc(job.url, quote=True)}">Відкрити вакансію</a>'
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    missing = [k for k in ("GEMINI_API_KEY", "TG_BOT_TOKEN", "TG_CHAT_ID") if not os.environ.get(k)]
    if missing:
        log.error("Missing secrets: %s", ", ".join(missing))
        sys.exit(1)

    cfg = load_config()
    if cfg["paused"]:
        log.info("Agent is paused (/resume in Telegram to continue)")
        return

    seen = read_json(SEEN_FILE, {})
    state = read_json(STATE_FILE, {})
    if not isinstance(seen, dict):
        seen = {}
    if not isinstance(state, dict):
        state = {}
    now = utcnow_iso()
    stats = {"time": now, "collected": 0, "candidates": 0, "evaluated": 0, "sent": 0, "errors": []}

    try:
        jobs, counts, errors = collect(cfg)
        stats["collected"], stats["errors"] = len(jobs), errors

        stop_rx = compile_stop_words(cfg["stop_words"])
        unique: dict[str, Job] = {}
        for job in jobs:
            if job.id in unique:
                continue
            if job.id in seen:
                seen[job.id] = now  # still open: keep it in memory so it never comes back
                continue
            if not job.title or (stop_rx and stop_rx.search(job.title)) or not sources.is_python_related(job):
                seen[job.id] = now
                continue
            unique[job.id] = job
        candidates = sorted(unique.values(), key=rank)
        stats["candidates"] = len(candidates)
        log.info("Candidates after filtering: %d", len(candidates))

        if candidates:
            client = make_client()
            sent_registry: dict = state.setdefault("sent", {})
            failures = 0
            for job in candidates[: cfg["max_llm_per_run"]]:
                ev = evaluate(client, cfg, job)
                if ev is None:
                    failures += 1
                    if failures >= 5:
                        log.error("Gemini keeps failing, stopping this run")
                        stats["errors"].append("gemini")
                        break
                    continue  # not marked as seen: retried on the next run
                failures = 0
                stats["evaluated"] += 1
                log.info("[%d/10] %s @ %s", ev.score, job.title, job.company)
                delivered = True
                if ev.score >= cfg["min_score"]:
                    delivered = send_message(job_message(job, ev), job_keyboard(job.id)) is not None
                    if delivered:
                        stats["sent"] += 1
                        sent_registry[job.id] = {"title": job.title, "company": job.company, "url": job.url}
                if delivered:
                    seen[job.id] = now
                time.sleep(cfg["llm_pause_sec"])
            state["sent"] = dict(list(sent_registry.items())[-SENT_MEMORY:])
            left = len(candidates) - cfg["max_llm_per_run"]
            if left > 0:
                log.info("%d candidates left for the next run", left)
    finally:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)).isoformat()
        write_json(SEEN_FILE, {k: v for k, v in seen.items() if v >= cutoff}, sort_keys=True)
        state["last_run"] = stats
        write_json(STATE_FILE, state)

    log.info("Done: collected=%d candidates=%d evaluated=%d sent=%d",
             stats["collected"], stats["candidates"], stats["evaluated"], stats["sent"])
    if cfg["send_summary"]:
        text = (f"📊 <b>Запуск завершено</b>\nЗібрано: {stats['collected']} · нових: {stats['candidates']} · "
                f"оцінено: {stats['evaluated']} · надіслано: {stats['sent']}")
        if stats["errors"]:
            text += "\n⚠️ Збої: " + esc(", ".join(stats["errors"][:10]))
        send_message(text)


if __name__ == "__main__":
    main()
