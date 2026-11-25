"""
Telegram command handler.

There is no always-on server: every run of the workflow calls getUpdates, handles
the commands that arrived since the last run, saves the changes to the repo
(config.json / companies.json / state.json) and exits. Only the owner chat
(TG_CHAT_ID) is obeyed.
"""
from __future__ import annotations

import os
import sys

import requests

from common import (
    STATE_FILE, CONFIG_FILE, esc, fmt_time, job_keyboard, load_companies,
    load_config, log, read_json, save_companies, send_message, tg, utcnow_iso, write_json,
)
from sources import fetch_company_raw, is_python_related, resolve_company

SOURCE_NAMES = {
    "jobspy": "LinkedIn + Indeed",
    "aggregators": "Remote-агрегаторы (Remotive, Arbeitnow, Jobicy, Working Nomads, RemoteOK)",
    "rss": "RSS (WeWorkRemotely, Python.org, DOU, Djinni)",
    "companies": "Сайты компаний",
}

BOT_COMMANDS = [
    ("help", "Все команды"),
    ("status", "Состояние и итоги последнего запуска"),
    ("run", "Запустить поиск сейчас"),
    ("pause", "Поставить поиск на паузу"),
    ("resume", "Продолжить поиск"),
    ("minscore", "Порог оценки ИИ, например /minscore 7"),
    ("companies", "Список компаний"),
    ("addcompany", "Добавить компанию: ссылка или название"),
    ("delcompany", "Удалить компанию"),
    ("terms", "Поисковые запросы"),
    ("stops", "Стоп-слова в названии"),
    ("saved", "Сохранённые вакансии"),
    ("applied", "Вакансии, куда откликнулись"),
]

HELP = """<b>🤖 Агент поиска вакансий</b>
Каждые 3 часа я собираю вакансии (Python backend, джун/трейни, remote) с LinkedIn, Indeed, remote-досок, RSS и сайтов компаний, отсеиваю лишнее, оцениваю через ИИ и присылаю подходящие.

<b>Управление</b>
/status — состояние и итоги последнего запуска
/run — запустить поиск вне расписания
/pause · /resume — пауза / продолжить
/minscore 7 — порог оценки ИИ (1–10)
/hours 24 — за сколько часов брать свежие вакансии
/summary on|off — краткий итог после каждого запуска
/source rss off — включить/выключить источник (jobspy, aggregators, rss, companies)

<b>Поиск</b>
/terms — поисковые запросы для LinkedIn/Indeed
/addterm junior django developer
/delterm 2 — по номеру или тексту
/stops — стоп-слова в названии вакансии
/addstop php · /delstop senior
/profile — профиль, по которому ИИ оценивает вакансии
/setprofile текст — заменить профиль

<b>Компании</b>
/companies — список
/addcompany https://company.com/careers — по ссылке на страницу вакансий
/addcompany greenhouse:gitlab — если знаете систему и название
/addcompany gitlab — попробую найти сам
/delcompany gitlab — удалить

<b>Вакансии</b>
Под каждой вакансией кнопки: ⭐ сохранить, ✅ откликнулся, 🙈 скрыть.
/saved — сохранённые · /applied — отклики

ℹ️ Бот работает без сервера: команды обрабатываются при ближайшей проверке (обычно в течение 30 минут)."""


class Ctx:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.companies = load_companies()
        self.state = read_json(STATE_FILE, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.chat_id = str(os.environ["TG_CHAT_ID"])
        self.cfg_dirty = self.companies_dirty = self.state_dirty = False
        self.run_now = False

    def save(self) -> None:
        if self.cfg_dirty:
            write_json(CONFIG_FILE, self.cfg)
        if self.companies_dirty:
            save_companies(self.companies)
        if self.state_dirty:
            write_json(STATE_FILE, self.state)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _remove(items: list, arg: str):
    arg = arg.strip()
    if arg.isdigit() and 1 <= int(arg) <= len(items):
        return items.pop(int(arg) - 1)
    for i, item in enumerate(items):
        if str(item).lower() == arg.lower():
            return items.pop(i)
    return None


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {esc(str(x))}" for i, x in enumerate(items, 1)) or "— пусто —"


def _err(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def _link_list(items: dict, limit: int = 30) -> str:
    rows = list(items.values())[-limit:]
    return "\n".join(
        f'{i}. <a href="{esc(r["url"], quote=True)}">{esc(r["title"])}</a> — {esc(r.get("company") or "—")}'
        for i, r in enumerate(rows, 1)
    )


# --------------------------------------------------------------------------- #
# commands: each returns the reply text
# --------------------------------------------------------------------------- #
def cmd_start(ctx: Ctx, arg: str) -> str:
    if not ctx.state.get("commands_set"):
        ok = tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
        if ok:
            ctx.state["commands_set"] = True
            ctx.state_dirty = True
    return HELP


def cmd_status(ctx: Ctx, arg: str) -> str:
    cfg, tz = ctx.cfg, ctx.cfg.get("timezone", "Europe/Kyiv")
    srcs = " · ".join(f"{k} {'✅' if cfg['sources'].get(k) else '⛔'}" for k in SOURCE_NAMES)
    lines = [
        "<b>📊 Статус</b>",
        f"Поиск: {'⏸ на паузе' if cfg['paused'] else '▶️ работает'}",
        f"Порог оценки: ≥ {cfg['min_score']} · свежесть: {cfg['hours_old']} ч",
        f"Источники: {srcs}",
        f"Компаний: {len(ctx.companies)} · запросов: {len(cfg['search_terms'])} · стоп-слов: {len(cfg['stop_words'])}",
        f"Сохранено: {len(ctx.state.get('saved', {}))} · откликов: {len(ctx.state.get('applied', {}))}",
    ]
    last = ctx.state.get("last_run")
    if last:
        lines += [
            "",
            f"<b>Последний запуск</b> {fmt_time(last.get('time'), tz)}",
            f"Собрано: {last.get('collected', 0)} · новых: {last.get('candidates', 0)} · "
            f"оценено: {last.get('evaluated', 0)} · отправлено: {last.get('sent', 0)}",
        ]
        if last.get("errors"):
            lines.append("⚠️ Сбои: " + esc(", ".join(last["errors"][:10])))
    else:
        lines += ["", "Запусков ещё не было."]
    return "\n".join(lines)


def cmd_run(ctx: Ctx, arg: str) -> str:
    if ctx.cfg["paused"]:
        return "⏸ Поиск на паузе. Сначала /resume."
    ctx.run_now = True
    return "🚀 Запускаю поиск прямо сейчас. Результаты придут через несколько минут."


def cmd_pause(ctx: Ctx, arg: str) -> str:
    ctx.cfg["paused"], ctx.cfg_dirty = True, True
    return "⏸ Поиск на паузе. Вернуть: /resume"


def cmd_resume(ctx: Ctx, arg: str) -> str:
    ctx.cfg["paused"], ctx.cfg_dirty = False, True
    return "▶️ Поиск продолжен. Ближайший запуск по расписанию или /run."


def cmd_minscore(ctx: Ctx, arg: str) -> str:
    if not arg.strip().isdigit() or not 1 <= int(arg) <= 10:
        return f"Сейчас порог ≥ {ctx.cfg['min_score']}. Изменить: /minscore 6 (число от 1 до 10)."
    ctx.cfg["min_score"], ctx.cfg_dirty = int(arg), True
    return f"✅ Теперь присылаю вакансии с оценкой ≥ {int(arg)}."


def cmd_hours(ctx: Ctx, arg: str) -> str:
    if not arg.strip().isdigit() or not 1 <= int(arg) <= 168:
        return f"Сейчас беру вакансии за {ctx.cfg['hours_old']} ч. Изменить: /hours 48 (1–168)."
    ctx.cfg["hours_old"], ctx.cfg_dirty = int(arg), True
    return f"✅ Свежесть вакансий: {int(arg)} ч."


def cmd_summary(ctx: Ctx, arg: str) -> str:
    val = arg.strip().lower()
    if val not in ("on", "off"):
        return f"Итог после запуска: {'вкл' if ctx.cfg['send_summary'] else 'выкл'}. Изменить: /summary on или /summary off"
    ctx.cfg["send_summary"], ctx.cfg_dirty = val == "on", True
    return f"✅ Итог после каждого запуска: {'включён' if val == 'on' else 'выключен'}."


def cmd_source(ctx: Ctx, arg: str) -> str:
    parts = arg.lower().split()
    if len(parts) != 2 or parts[0] not in SOURCE_NAMES or parts[1] not in ("on", "off"):
        rows = "\n".join(f"{'✅' if ctx.cfg['sources'].get(k) else '⛔'} <code>{k}</code> — {v}" for k, v in SOURCE_NAMES.items())
        return f"<b>Источники</b>\n{rows}\n\nИзменить: /source rss off"
    ctx.cfg["sources"][parts[0]], ctx.cfg_dirty = parts[1] == "on", True
    return f"✅ {parts[0]}: {'включён' if parts[1] == 'on' else 'выключен'}."


def _list_cmd(key: str, title: str):
    def handler(ctx: Ctx, arg: str) -> str:
        return f"<b>{title}</b>\n{_numbered(ctx.cfg[key])}"
    return handler


def _add_cmd(key: str, label: str, lower: bool):
    def handler(ctx: Ctx, arg: str) -> str:
        value = arg.strip().lower() if lower else arg.strip()
        if not value or len(value) > 100:
            return f"Напишите, что добавить, например: /{'addstop php' if lower else 'addterm junior django developer'}"
        if value.lower() in (str(x).lower() for x in ctx.cfg[key]):
            return f"Уже есть: {esc(value)}"
        ctx.cfg[key].append(value)
        ctx.cfg_dirty = True
        return f"✅ Добавил {label}: {esc(value)}"
    return handler


def _del_cmd(key: str, label: str):
    def handler(ctx: Ctx, arg: str) -> str:
        removed = _remove(ctx.cfg[key], arg) if arg.strip() else None
        if removed is None:
            return f"Не нашёл. Укажите номер или текст из списка."
        ctx.cfg_dirty = True
        return f"🗑 Удалил {label}: {esc(str(removed))}"
    return handler


def cmd_profile(ctx: Ctx, arg: str) -> str:
    return "<b>Профиль для ИИ</b>\n" + esc(ctx.cfg["profile"])[:3500] + "\n\nЗаменить: /setprofile новый текст"


def cmd_setprofile(ctx: Ctx, arg: str) -> str:
    text = arg.strip()
    if len(text) < 30:
        return "Опишите профиль подробнее (роль, стек, формат работы, английский, опыт). Пример смотрите в /profile."
    ctx.cfg["profile"], ctx.cfg_dirty = text[:3500], True
    return "✅ Профиль обновлён. ИИ будет оценивать вакансии по нему."


def cmd_companies(ctx: Ctx, arg: str) -> str:
    if not ctx.companies:
        return "Список пуст. Добавить: /addcompany https://company.com/careers"
    rows = [
        f"{i}. {esc(c.get('name', '?'))} <i>({esc(c['ats'])})</i>"
        for i, c in enumerate(ctx.companies, 1)
    ]
    return f"<b>Компании ({len(rows)})</b>\n" + "\n".join(rows) + "\n\nДобавить: /addcompany ссылка\nУдалить: /delcompany название"


def cmd_addcompany(ctx: Ctx, arg: str) -> str:
    if not arg.strip():
        return ("Пришлите ссылку на страницу вакансий компании или название:\n"
                "/addcompany https://company.com/careers\n/addcompany greenhouse:gitlab\n/addcompany gitlab")
    comp, error = resolve_company(arg)
    if comp is None:
        return f"❌ {esc(error or 'не получилось')}"
    ident = str(comp.get("token") or comp.get("url")).lower()
    if any(c["ats"] == comp["ats"] and str(c.get("token") or c.get("url")).lower() == ident for c in ctx.companies):
        return f"Эта компания уже в списке: {esc(comp['name'])}."
    try:
        raw = fetch_company_raw(comp)
    except Exception as e:  # noqa: BLE001
        return f"❌ Не удалось получить вакансии ({_err(e)}). Не добавил."
    if not raw:
        hint = " Возможно, страница строится JavaScript-ом." if comp["ats"] == "custom" else ""
        return f"❌ Вакансий не найдено, не добавил.{hint}"
    related = [j for j in raw if is_python_related(j)]
    ctx.companies.append(comp)
    ctx.companies_dirty = True
    mode = ("общий режим: ищу ссылки со словами python/backend" if comp["ats"] == "custom"
            else f"система найма: {comp['ats']}")
    return (f"✅ Добавил <b>{esc(comp['name'])}</b> ({mode}).\n"
            f"Сейчас открыто вакансий: {len(raw)}, из них про Python: {len(related)}.")


def cmd_delcompany(ctx: Ctx, arg: str) -> str:
    key = arg.strip().lower()
    if not key:
        return "Укажите номер или название: /delcompany gitlab"
    if key.isdigit() and 1 <= int(key) <= len(ctx.companies):
        removed = ctx.companies.pop(int(key) - 1)
    else:
        idx = next((i for i, c in enumerate(ctx.companies)
                    if key in (str(c.get("name", "")).lower(), str(c.get("token", "")).lower())), None)
        removed = ctx.companies.pop(idx) if idx is not None else None
    if removed is None:
        return "Не нашёл такую компанию. Список: /companies"
    ctx.companies_dirty = True
    return f"🗑 Удалил: {esc(removed.get('name', '?'))}"


def cmd_saved(ctx: Ctx, arg: str) -> str:
    items = ctx.state.get("saved", {})
    return "<b>⭐ Сохранённые</b>\n" + _link_list(items) if items else "Пока ничего не сохранено (кнопка ⭐ под вакансией)."


def cmd_applied(ctx: Ctx, arg: str) -> str:
    items = ctx.state.get("applied", {})
    return "<b>✅ Отклики</b>\n" + _link_list(items) if items else "Пока нет откликов (кнопка ✅ под вакансией)."


COMMANDS = {
    "start": cmd_start, "help": cmd_start, "status": cmd_status, "run": cmd_run,
    "pause": cmd_pause, "resume": cmd_resume, "minscore": cmd_minscore, "hours": cmd_hours,
    "summary": cmd_summary, "source": cmd_source,
    "terms": _list_cmd("search_terms", "Поисковые запросы"),
    "addterm": _add_cmd("search_terms", "запрос", lower=False),
    "delterm": _del_cmd("search_terms", "запрос"),
    "stops": _list_cmd("stop_words", "Стоп-слова (в названии вакансии)"),
    "addstop": _add_cmd("stop_words", "стоп-слово", lower=True),
    "delstop": _del_cmd("stop_words", "стоп-слово"),
    "profile": cmd_profile, "setprofile": cmd_setprofile,
    "companies": cmd_companies, "addcompany": cmd_addcompany, "delcompany": cmd_delcompany,
    "saved": cmd_saved, "applied": cmd_applied,
}


# --------------------------------------------------------------------------- #
# updates
# --------------------------------------------------------------------------- #
def handle_message(msg: dict, ctx: Ctx) -> None:
    if str(msg.get("chat", {}).get("id")) != ctx.chat_id:
        return  # not the owner: ignore silently
    parts = (msg.get("text") or "").strip().split(None, 1)
    if not parts:
        return
    if not parts[0].startswith("/"):
        send_message("Я понимаю команды. Список: /help")
        return
    cmd = parts[0][1:].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    send_message(handler(ctx, arg) if handler else "Не знаю такую команду. Список: /help")


def handle_callback(cb: dict, ctx: Ctx) -> None:
    msg = cb.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != ctx.chat_id:
        return
    action, _, job_id = (cb.get("data") or "").partition(":")
    job = ctx.state.get("sent", {}).get(job_id)
    if job is None:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Эта вакансия уже не в памяти бота")
        return
    saved, applied = ctx.state.setdefault("saved", {}), ctx.state.setdefault("applied", {})
    ctx.state_dirty = True
    if action == "hide":
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Скрыто")
        tg("deleteMessage", chat_id=ctx.chat_id, message_id=msg.get("message_id"))
        return
    store, labels = {"save": (saved, ("⭐ Убрано из сохранённых", "⭐ Сохранено")),
                     "applied": (applied, ("Отметка снята", "✅ Отмечено как отклик"))}.get(action, (None, ()))
    if store is None:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        return
    if job_id in store:
        del store[job_id]
        toast = labels[0]
    else:
        store[job_id] = {**job, "at": utcnow_iso()}
        toast = labels[1]
    tg("answerCallbackQuery", callback_query_id=cb["id"], text=toast)
    tg("editMessageReplyMarkup", chat_id=ctx.chat_id, message_id=msg.get("message_id"),
       reply_markup=job_keyboard(job_id, saved=job_id in saved, applied=job_id in applied))


def set_output(run_now: bool) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"run_now={'true' if run_now else 'false'}\n")


def main() -> None:
    if not os.environ.get("TG_BOT_TOKEN") or not os.environ.get("TG_CHAT_ID"):
        log.error("TG_BOT_TOKEN / TG_CHAT_ID are not set")
        set_output(False)
        sys.exit(1)
    ctx = Ctx()
    updates = tg("getUpdates", offset=ctx.state.get("tg_offset", 0), timeout=0,
                 allowed_updates=["message", "callback_query"])
    if updates is None:
        set_output(False)
        sys.exit(1)
    log.info("Telegram updates: %d", len(updates))
    for update in updates:
        ctx.state["tg_offset"], ctx.state_dirty = update["update_id"] + 1, True  # never get stuck on a bad update
        try:
            if "message" in update:
                handle_message(update["message"], ctx)
            elif "callback_query" in update:
                handle_callback(update["callback_query"], ctx)
        except Exception:  # noqa: BLE001
            log.exception("Failed to handle update %s", update.get("update_id"))
            send_message("⚠️ Не удалось выполнить команду. Подробности в логах GitHub Actions.")
    ctx.save()
    set_output(ctx.run_now)


if __name__ == "__main__":
    main()
