"""LinkedIn (EU) -> Telegram: бот слушает команды в чате и по ним запускает/останавливает поиск вакансий."""
import html
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError, sync_playwright

HERE = Path(__file__).resolve().parent  # все файлы бота лежат рядом с main.py, откуда бы его ни запустили
ENV_PATH = HERE / ".env"
load_dotenv(ENV_PATH)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEED_KEYWORDS = [k.strip() for k in os.getenv("SEARCH_KEYWORDS", "").split(",") if k.strip()]  # стартовый список, меняется /keywords
LOCATION = os.getenv("LOCATION", "European Union")
GEO_ID = os.getenv("GEO_ID", "91000002")  # geoId из ссылки LinkedIn (совпадает с 91000000 по выдаче)
TIME_RANGE = int(os.getenv("TIME_RANGE_SECONDS", "86400"))  # вакансии за последние N секунд
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))  # 10 вакансий на страницу; LinkedIn отдаёт максимум ~1000
DELAY_MIN = float(os.getenv("DELAY_MIN", "2"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "6"))
INTERVAL_MIN = int(os.getenv("INTERVAL_MINUTES", "0"))  # 0 = один проход после /go, >0 = повторять каждые N минут
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
DB_PATH = os.getenv("DB_PATH") or str(HERE / "seen_jobs.db")
REMOTE_ONLY = os.getenv("REMOTE_ONLY", "false").lower() == "true"  # стартовое значение, меняется /remote
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()  # gemini | deepseek; нужен только если регулярки ничего не нашли
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
AI_ENABLED = bool(GEMINI_API_KEY if AI_PROVIDER == "gemini" else DEEPSEEK_API_KEY)
PERIODS = {"1h": 3600, "1d": 86400, "1w": 604800, "1m": 2592000}  # период для /period

BASE = "https://www.linkedin.com/jobs-guest/jobs/api"
API = f"https://api.telegram.org/bot{TOKEN}"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
TG_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z]\w{3,})|telegram\W{1,5}@([A-Za-z]\w{3,})", re.I)
PHONE_RE = re.compile(r"\+\d[\d\s().-]{7,16}\d")
LINK_RE = re.compile(r"https?://[^\s\"'<>]+[^\s\"'<>.,;)]")  # формы отклика / страницы карьеры из текста
# Номер без «+» берём только после слов вроде tel/phone/call: иначе даты и индексы дадут ложные срабатывания
LOCAL_PHONE_RE = re.compile(
    r"(?:tel|phone|call|mobile|mob|whatsapp|telefon|téléphone|telefono|teléfono|tlf|handy)\w*"
    r"(?:<[^>]+>|[\s:.\-–])*(\(?0\)?\d[\d\s().-]{6,14}\d)", re.I)
DIAL_CODES = {  # код страны по последней части локации вакансии («Berlin, Berlin, Germany»)
    "austria": "43", "belgium": "32", "bulgaria": "359", "croatia": "385", "cyprus": "357", "czechia": "420",
    "czech republic": "420", "denmark": "45", "estonia": "372", "finland": "358", "france": "33", "germany": "49",
    "greece": "30", "hungary": "36", "ireland": "353", "italy": "39", "latvia": "371", "lithuania": "370",
    "luxembourg": "352", "malta": "356", "netherlands": "31", "poland": "48", "portugal": "351", "romania": "40",
    "slovakia": "421", "slovenia": "386", "spain": "34", "sweden": "46",
}

HELP = ("/go — начать поиск\n"
        "/stop — остановить поиск\n"
        "/keywords слово1, слово2 — задать ключевые слова\n"
        "/keywords all — искать все вакансии без фильтра\n"
        "/keywords — показать текущие\n"
        "/mode remote — только чистый ремот\n"
        "/mode hybrid — только гибрид\n"
        "/mode both — ремот или гибрид\n"
        "/mode any — любой формат\n"
        "/period 1d — за какой период искать: 1h, 1d, 1w, 1m\n"
        "/maxcompany 2 — не больше N вакансий одной компании за проход (0 = без лимита)\n"
        "/delay 60 — пауза в секундах между карточками\n"
        "/status — что сейчас происходит")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobs")

stop_event = threading.Event()  # сигнал остановки для потока поиска
worker = None  # поток поиска, создаётся по /go


def uniq(items):
    return list(dict.fromkeys(items))


def local_phone(num, dial):
    """«030 1234567» + код страны 49 → «+49 30 1234567». Без кода страны возвращает номер как написан."""
    num = re.sub(r"\s+", " ", num).strip()
    if not dial:
        return num
    if dial == "39":  # в Италии ноль остаётся и в международном формате
        return f"+39 {num}"
    return f"+{dial} " + re.sub(r"^\(?0\)?[\s.-]*", "", num)


def dial_code(location):
    return DIAL_CODES.get(location.split(",")[-1].strip().lower())


def extract_contacts(text, dial=None):
    emails = uniq(e for e in EMAIL_RE.findall(text) if not e.lower().endswith((".png", ".jpg", ".gif", ".svg")))
    telegram = uniq("@" + (a or b) for a, b in TG_RE.findall(text))
    phones = [re.sub(r"\s+", " ", p).strip() for p in PHONE_RE.findall(text)]
    phones = uniq(phones + [local_phone(m, dial) for m in LOCAL_PHONE_RE.findall(text)])
    links = uniq(u for u in LINK_RE.findall(text) if not re.search(r"linkedin\.com|t\.me/|telegram\.me/", u))
    found = {"emails": emails, "telegram": telegram, "phones": phones, "links": links}
    return {k: v for k, v in found.items() if v}


def human_pause(a=DELAY_MIN, b=DELAY_MAX):
    stop_event.wait(random.uniform(a, b))  # прерывается по /stop


def fetch(page, url):
    """Открывает URL; на 429/999/5xx — экспоненциальный бэкофф. False = данных нет."""
    for attempt in range(4):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightError as e:  # таймаут или обрыв сети: повторяем, а не роняем весь проход
            log.warning("Страница не загрузилась (%s), повторю", type(e).__name__)
            stop_event.wait(10)
            continue
        status = resp.status if resp else 0
        if status == 200:
            page.mouse.move(random.randint(50, 800), random.randint(50, 600))
            page.mouse.wheel(0, random.randint(200, 1200))
            return True
        if status in (429, 999) or status >= 500:
            wait = 30 * 2 ** attempt + random.uniform(0, 15)
            log.warning("HTTP %s, жду %.0f с", status, wait)
            stop_event.wait(wait)
            continue
        return False  # 400/404 — конец выдачи
    return False


def text_of(el, selector):
    node = el.query_selector(selector)
    return node.inner_text().strip() if node else ""


def search(page, keyword, period):
    """Отдаёт вакансии страница за страницей, чтобы обрабатывать их по мере загрузки."""
    for p in range(MAX_PAGES):
        url = (f"{BASE}/seeMoreJobPostings/search?keywords={quote_plus(keyword)}&location={quote_plus(LOCATION)}"
               f"&geoId={GEO_ID}&f_TPR=r{period}&start={p * 10}")
        if not fetch(page, url):
            break
        cards = page.query_selector_all("[data-entity-urn*='jobPosting']")
        if not cards:
            break
        jobs = []
        for c in cards:
            job_id = (c.get_attribute("data-entity-urn") or "").rsplit(":", 1)[-1]
            if job_id.isdigit():
                jobs.append({
                    "id": job_id,
                    "title": text_of(c, ".base-search-card__title"),
                    "company": text_of(c, ".base-search-card__subtitle"),
                    "location": text_of(c, ".job-search-card__location"),
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                })
        log.info("'%s': страница %d, карточек %d", keyword or "*", p + 1, len(jobs))
        yield from jobs
        human_pause()


def enrich(page, job):
    """Дотягивает описание, контакты, внешние ссылки и рекрутера."""
    job.update(summary="", contacts={}, text="")
    if not fetch(page, f"{BASE}/jobPosting/{job['id']}"):
        return None  # не открылась: вызывающий не должен считать вакансию «без контактов»
    desc = page.query_selector(".show-more-less-html__markup")
    if not desc:
        return None  # страница без описания (удалена или LinkedIn отдал урезанную): не считаем вакансию проверенной
    if desc:
        job["text"] = desc.inner_text()
        bullets = [li.inner_text().strip() for li in desc.query_selector_all("li")][:5]
        job["summary"] = ("\n".join("• " + b[:150] for b in bullets if b)
                          or re.sub(r"\s+", " ", desc.inner_text())[:400])
        job["contacts"] = extract_contacts(html.unescape(desc.inner_html()), dial_code(job["location"]))

    rec = page.query_selector(".message-the-recruiter")
    if rec:
        link = rec.query_selector("a[href*='/in/']")
        name = text_of(rec, ".base-main-card__title") or (link.inner_text().strip() if link else "")
        if name:
            job["contacts"]["recruiter"] = (name, link.get_attribute("href").split("?")[0] if link else "")

    if AI_ENABLED and job["text"] and not has_email_or_phone(job):
        job["contacts"].update(ai_contacts(job["text"]))
    return job


def ask_llm(prompt):
    """Запрос к выбранной модели, ответ — строка с JSON."""
    if AI_PROVIDER == "deepseek":
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={"model": DEEPSEEK_MODEL, "temperature": 0, "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
        timeout=60)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def ai_contacts(text):
    """LLM ищет email/телефон в тексте вакансии. Берём только то, что буквально есть в тексте, без выдумок."""
    prompt = ("Найди в тексте вакансии контакты для отклика: email и телефоны. Верни JSON "
              '{"emails": [], "phones": []}. Включай только значения, которые буквально есть в тексте. '
              "Если контактов нет, верни пустые списки.\n\nТЕКСТ:\n" + text[:6000])
    try:
        data = json.loads(ask_llm(prompt))
    except Exception as e:
        log.warning("%s не ответил: %s", AI_PROVIDER, e)
        return {}
    low = text.lower()
    digits = re.sub(r"\D", "", text)
    emails = [e for e in data.get("emails", []) if isinstance(e, str) and e.lower() in low]
    phones = [p for p in data.get("phones", []) if isinstance(p, str) and re.sub(r"\D", "", p)[-7:] in digits]
    found = {"emails": uniq(emails), "phones": uniq(phones)}
    return {k: v for k, v in found.items() if v}


def has_email_or_phone(job):
    return bool(job["contacts"].get("emails") or job["contacts"].get("phones"))


REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
# только явные «не удалённая»: просто слово on-site встречается и в обязанностях («on-site visits»)
ONSITE_RE = re.compile(
    r"\bnot (?:a )?remote\b|\bno remote\b|\bnon[- ]remote\b|\bremote (?:work )?(?:is )?not (?:possible|available|offered)\b"
    r"|\b(?:100% |fully |strictly )on[- ]?site\b|\bon[- ]?site only\b|\bin[- ]office only\b|\boffice[- ]based\b", re.I)
# слова, которые уйдут в запрос LinkedIn: LinkedIn-фильтр f_WT здесь не работает
MODE_QUERIES = {"remote": ["remote"], "hybrid": ["hybrid"], "both": ["remote", "hybrid"], "any": [""]}
MODE_ALIASES = {"remote": "remote", "ремот": "remote", "hybrid": "hybrid", "гибрид": "hybrid",
                "both": "both", "оба": "both", "any": "any", "все": "any"}


def work_match(job, mode):
    """Фраза, по которой вакансия подходит под формат работы, или None."""
    if mode == "any":
        return "любая работа"
    fields = " ".join([job["title"], job["location"], job["text"]])
    remote = REMOTE_RE.search(fields)
    hybrid = HYBRID_RE.search(fields)
    onsite = ONSITE_RE.search(fields)
    if mode == "hybrid":
        return hybrid.group(0) if hybrid else None
    if mode == "remote":
        return remote.group(0) if remote and not hybrid and not onsite else None
    if hybrid:  # both
        return hybrid.group(0)
    return remote.group(0) if remote and not onsite else None


def reject_reason(job, mode):
    """Почему вакансия не прошла по формату работы: для лога, чтобы было понятно."""
    fields = " ".join([job["title"], job["location"], job["text"]])
    remote, hybrid, onsite = REMOTE_RE.search(fields), HYBRID_RE.search(fields), ONSITE_RE.search(fields)
    if mode == "hybrid":
        return "в тексте нет слова hybrid"
    if mode == "remote" and hybrid:
        return "это гибрид, а режим remote"
    if remote and onsite:
        return f"есть «{onsite.group(0)}», офисная"
    return "в тексте нет слов remote/hybrid"


def format_job(job):
    e = html.escape
    lines = [f"<b>💼 {e(job['title'])}</b>", f"🏢 {e(job['company'])}", f"📍 {e(job['location'])}"]
    if job.get("remote_note"):
        lines.append(f"🌍 {e(job['remote_note'])}")
    if job["summary"]:
        lines += ["", e(job["summary"])]
    c = job["contacts"]
    if c:
        lines.append("")
    for mail in c.get("emails", []):
        lines.append(f"📧 {e(mail)}")
    for tg in c.get("telegram", []):
        lines.append(f'✈️ <a href="https://t.me/{e(tg[1:])}">{e(tg)}</a>')
    for phone in c.get("phones", []):
        lines.append(f"📞 {e(phone)}")
    if "recruiter" in c:
        name, link = c["recruiter"]
        lines.append(f'👤 <a href="{e(link)}">{e(name)}</a>' if link else f"👤 {e(name)}")
    for link in c.get("links", [])[:3]:
        lines.append(f'📝 <a href="{e(link)}">{e(urlparse(link).netloc)}</a>')
    lines += ["", f'<a href="{e(job["url"])}">🔗 Открыть вакансию</a>']
    return "\n".join(lines)[:4096]


def tg(method, **payload):
    """Вызов Telegram Bot API; при 429 ждёт и повторяет. None = не получилось."""
    for _ in range(5):
        try:
            r = requests.post(f"{API}/{method}", json=payload, timeout=40)
        except requests.RequestException as e:
            # ponytail: текст ошибки requests содержит URL с токеном, поэтому логируем только тип
            log.warning("Telegram %s: сеть (%s), повторю через 5 с", method, type(e).__name__)
            time.sleep(5)
            continue
        if r.ok:
            return r.json()
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
            continue
        log.error("Telegram %s %s: %s", method, r.status_code, r.text)
        return None
    return None


def send(text):
    """Карточка вакансии в HTML."""
    ok = tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    time.sleep(max(1.1, float(get_setting("delay", 1.1))))  # не меньше 1.1 с: лимит Telegram ~1 msg/s в чат
    return ok is not None


def reply(text):
    """Обычный текст ответа на команду."""
    tg("sendMessage", chat_id=CHAT_ID, text=text)


def db_conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, url TEXT, sent_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS fingerprints (fp TEXT PRIMARY KEY, seen_at TEXT)")
    return c


def fingerprint(job):
    """Отпечаток «компания + название + город»: LinkedIn часто публикует одну вакансию под разными ID."""
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    return "|".join(norm(job[k]) for k in ("company", "title", "location"))


def is_duplicate(db, job):
    # ponytail: окно 30 дней, чтобы настоящая повторная публикация через месяц не терялась навсегда
    return bool(db.execute("SELECT 1 FROM fingerprints WHERE fp=? AND seen_at > datetime('now', '-30 days')",
                           (fingerprint(job),)).fetchone())


def mark_seen(db, job):
    db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, datetime('now'))", (job["id"], job["url"]))
    db.execute("INSERT OR REPLACE INTO fingerprints VALUES (?, datetime('now'))", (fingerprint(job),))
    db.commit()


def get_keywords():
    with closing(db_conn()) as db:
        row = db.execute("SELECT value FROM settings WHERE key='keywords'").fetchone()
    return json.loads(row[0]) if row else SEED_KEYWORDS


def set_keywords(kws):
    with closing(db_conn()) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('keywords', ?)", (json.dumps(kws, ensure_ascii=False),))
        db.commit()


def get_mode():
    with closing(db_conn()) as db:
        row = db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
    return row[0] if row else ("remote" if REMOTE_ONLY else "any")


def set_mode(mode):
    with closing(db_conn()) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('mode', ?)", (mode,))
        db.commit()


def get_setting(key, default):
    with closing(db_conn()) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with closing(db_conn()) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))
        db.commit()


def get_period():
    with closing(db_conn()) as db:
        row = db.execute("SELECT value FROM settings WHERE key='period'").fetchone()
    return int(row[0]) if row else TIME_RANGE


def set_period(seconds):
    with closing(db_conn()) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('period', ?)", (str(seconds),))
        db.commit()


def period_name(seconds):
    return next((k for k, v in PERIODS.items() if v == seconds), f"{seconds} с")


def describe(kws):
    return ", ".join(kws) if kws else "все вакансии (без фильтра)"


def run_once(db, keywords, mode, period):
    cap = int(get_setting("max_company", 2))  # 0 = без лимита
    sent_by_company = {}  # сколько карточек уже ушло от каждой компании в этом проходе
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        try:
            queries = [" ".join(p for p in (kw, mod) if p) for kw in keywords or [""] for mod in MODE_QUERIES[mode]]
            for query in queries:
                if stop_event.is_set():
                    return
                ctx = browser.new_context(  # новый UA и размер окна на каждый поиск
                    user_agent=random.choice(USER_AGENTS), locale="en-US",
                    viewport={"width": random.randint(1280, 1920), "height": random.randint(720, 1080)})
                try:
                    page = ctx.new_page()
                    log.info("Ищу '%s' в %s (geoId %s, период %s)", query or "*", LOCATION, GEO_ID, period_name(period))
                    for job in search(page, query, period):
                        if stop_event.is_set():
                            return
                        if db.execute("SELECT 1 FROM seen WHERE id=?", (job["id"],)).fetchone():
                            continue
                        if is_duplicate(db, job):
                            log.info("  дубль (та же компания, название и город): %s — %s", job["title"], job["company"])
                            db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, datetime('now'))", (job["id"], job["url"]))
                            db.commit()
                            continue
                        company = job["company"].strip().lower()
                        if cap and sent_by_company.get(company, 0) >= cap:
                            continue  # не помечаем seen: уйдёт в следующий проход
                        human_pause()
                        if enrich(page, job) is None:
                            log.info("  не открылась, попробую в следующий проход: %s — %s", job["title"], job["company"])
                            continue  # не помечаем seen, иначе вакансия потеряется
                        evidence = work_match(job, mode)
                        if not evidence:
                            log.info("  не подходит (режим %s: %s): %s — %s", mode, reject_reason(job, mode),
                                     job["title"], job["company"])
                            continue  # не помечаем seen: при смене /mode такая вакансия должна подойти
                        if mode != "any":
                            job["remote_note"] = f"Формат: «{evidence}»"
                        if not has_email_or_phone(job):
                            log.info("  пропуск (нет email/телефона): %s — %s", job["title"], job["company"])
                            # ponytail: помечаем как просмотренную, чтобы не открывать её снова
                            mark_seen(db, job)
                            continue
                        if send(format_job(job)):
                            sent_by_company[company] = sent_by_company.get(company, 0) + 1
                            log.info("  отправлено: %s — %s", job["title"], job["company"])
                            mark_seen(db, job)
                finally:
                    ctx.close()
        finally:
            browser.close()


def run_search():
    log.info("Поиск запущен")
    try:
        while not stop_event.is_set():
            with closing(db_conn()) as db:
                run_once(db, get_keywords(), get_mode(), get_period())
            if not INTERVAL_MIN:
                break
            log.info("Жду %d мин до следующего прохода", INTERVAL_MIN)
            stop_event.wait(INTERVAL_MIN * 60)
    except Exception as e:
        log.exception("Поиск упал")
        reply(f"Поиск упал с ошибкой ({type(e).__name__}), подробности в логе. Отправьте /go, чтобы запустить снова.")
    finally:
        log.info("Поиск остановлен")
        reply("Поиск остановлен." if stop_event.is_set() else "Проход завершён.")


def running():
    return worker is not None and worker.is_alive()


def handle(text):
    global worker
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.split("@")[0].lower()  # /go@my_bot → /go
    arg = arg.strip()

    if cmd == "/go":
        if running():
            return "Уже ищу. /stop — остановить." if not stop_event.is_set() else "Ещё останавливаюсь, подожди немного."
        stop_event.clear()
        worker = threading.Thread(target=run_search, daemon=True)
        worker.start()
        return f"Запускаю поиск. Ключевые слова: {describe(get_keywords())}"

    if cmd == "/stop":
        if not running() or stop_event.is_set():
            return "Сейчас ничего не ищу."
        stop_event.set()
        return "Останавливаю. Текущая вакансия доработается и поиск встанет."

    if cmd == "/keywords":
        if not arg:
            return f"Сейчас: {describe(get_keywords())}"
        kws = [] if arg.lower() in ("all", "все") else [k.strip() for k in arg.split(",") if k.strip()]
        set_keywords(kws)
        return f"Сохранил: {describe(kws)}. Применю со следующего прохода (/stop, потом /go — сразу)."

    if cmd == "/mode":
        if not arg:
            return f"Формат работы сейчас: {get_mode()}. Задать: /mode remote, hybrid, both или any"
        if arg.lower() not in MODE_ALIASES:
            return "Доступно: /mode remote (чистый ремот), hybrid (гибрид), both (ремот или гибрид), any (любой)"
        set_mode(MODE_ALIASES[arg.lower()])
        return f"Формат работы: {MODE_ALIASES[arg.lower()]}. Применю со следующего прохода."

    if cmd == "/maxcompany":
        if not arg:
            return f"Не больше вакансий одной компании за проход: {get_setting('max_company', 2)} (0 = без лимита)"
        if not arg.isdigit():
            return "Напишите число: /maxcompany 2 (0 = без лимита)"
        set_setting("max_company", int(arg))
        return f"Лимит на компанию: {int(arg)}. Применю со следующего прохода."

    if cmd == "/delay":
        if not arg:
            return f"Пауза между карточками: {get_setting('delay', 1.1)} с"
        try:
            sec = max(1.1, min(float(arg), 3600))
        except ValueError:
            return "Напишите число секунд: /delay 60"
        set_setting("delay", sec)
        return f"Пауза между карточками: {sec} с"

    if cmd == "/period":
        if not arg:
            return f"Период сейчас: {period_name(get_period())}. Задать: /period 1h, 1d, 1w или 1m"
        if arg.lower() not in PERIODS:
            return "Доступно: /period 1h, 1d, 1w, 1m"
        set_period(PERIODS[arg.lower()])
        return f"Период: {arg.lower()}. Применю со следующего прохода."

    if cmd == "/status":
        searching = running() and not stop_event.is_set()
        return (f"Ищу: {'да' if searching else 'нет'}\n"
                f"Ключевые слова: {describe(get_keywords())}\n"
                f"Формат работы: {get_mode()}\n"
                f"Период: {period_name(get_period())}\n"
                f"AI для контактов: {AI_PROVIDER if AI_ENABLED else 'нет'}")

    return HELP


def skip_backlog():
    """Берёт id последнего сообщения, чтобы не выполнять старые команды. Неверный токен — сразу понятная ошибка."""
    while True:
        try:
            resp = requests.get(f"{API}/getUpdates", params={"offset": -1}, timeout=30).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Нет связи с Telegram (%s), повторю через 5 с", type(e).__name__)
            time.sleep(5)
            continue
        if not resp.get("ok"):
            raise SystemExit(f"Telegram отклонил запрос: {resp.get('description')}. Проверьте TELEGRAM_BOT_TOKEN в .env")
        return resp["result"][-1]["update_id"] + 1 if resp["result"] else None


def poll():
    """Long polling: слушает команды из чата TELEGRAM_CHAT_ID. Старые сообщения пропускает."""
    offset = skip_backlog()
    log.info("Слушаю команды. /help в чате — список")
    while True:
        try:
            resp = requests.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35).json()
        except (requests.RequestException, ValueError) as e:
            # ponytail: в тексте ошибки requests есть URL с токеном, поэтому пишем только тип ошибки
            log.warning("Нет связи с Telegram (%s), повторю через 5 с", type(e).__name__)
            time.sleep(5)
            continue
        if not resp.get("ok"):  # например 409: запущена вторая копия бота
            log.error("Telegram: %s", resp.get("description"))
            time.sleep(10)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue  # команды принимаем только из своего чата
            text = msg.get("text", "")
            if text.startswith("/"):
                try:
                    answer = handle(text)
                except Exception as e:
                    log.exception("Команда упала")
                    answer = f"Ошибка: {e}"
                reply(answer)


def build_env(token, chat_id, template):
    """Содержимое .env: токен и chat id плюс остальные настройки из .env.example."""
    kept = [ln for ln in template.splitlines() if not ln.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID="))]
    return f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n" + "\n".join(kept) + "\n"


def first_run_setup():
    """Новая машина без .env: спрашивает токен, сама находит chat id и создаёт .env."""
    print("Первый запуск на этой машине: настраиваю .env (один раз).")
    while True:
        token = input("Токен бота из @BotFather: ").strip()
        try:
            me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
        except (requests.RequestException, ValueError):
            print("Нет связи с Telegram, проверьте интернет и повторите.")
            continue
        if me.get("ok"):
            break
        print("Токен не подошёл, проверьте и повторите.")
    print(f"Бот @{me['result']['username']} найден. Напишите ему любое сообщение в Telegram (например /start), затем нажмите Enter.")
    while True:
        input()
        try:
            ups = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": -1}, timeout=20).json()
        except (requests.RequestException, ValueError):
            ups = {}
        if ups.get("result"):
            chat_id = ups["result"][-1].get("message", {}).get("chat", {}).get("id")
            if chat_id:
                break
        print("Сообщений от вас пока нет. Напишите боту в Telegram и нажмите Enter ещё раз.")
    example = HERE / ".env.example"
    ENV_PATH.write_text(build_env(token, chat_id, example.read_text(encoding="utf-8") if example.exists() else ""),
                        encoding="utf-8")
    print(f"Готово: {ENV_PATH.name} создан. Запускаю бота.\n")


def main():
    if not ENV_PATH.exists() and sys.stdin.isatty():
        try:
            first_run_setup()
        except EOFError:
            raise SystemExit("Нет ввода с клавиатуры. Скопируйте .env.example в .env и впишите токен и chat id вручную.")
        sys.exit(subprocess.call([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]))  # заново, уже с .env
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
    try:
        poll()
    except KeyboardInterrupt:
        stop_event.set()
        log.info("Выход")


if __name__ == "__main__":
    main()
