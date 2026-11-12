from main import extract_contacts, format_job

c = extract_contacts('Write to <a href="mailto:hr@acme.io">hr@acme.io</a>, Telegram: @acme_hr '
                     'or t.me/acmejobs, call +49 30 1234 5678. <img src="logo@2x.png">')
assert c == {"emails": ["hr@acme.io"], "telegram": ["@acme_hr", "@acmejobs"], "phones": ["+49 30 1234 5678"]}, c
assert extract_contacts("No contacts here, apply via LinkedIn.") == {}
assert extract_contacts('Apply: <a href="https://forms.gle/abc">https://forms.gle/abc</a>. '
                        'See https://www.linkedin.com/company/x') == {"links": ["https://forms.gle/abc"]}

msg = format_job({"title": "Dev <C++>", "company": "A&B", "location": "Berlin", "summary": "",
                  "url": "https://www.linkedin.com/jobs/view/1/", "contacts": c})
assert "Dev &lt;C++&gt;" in msg and "A&amp;B" in msg and 'href="https://t.me/acme_hr"' in msg
print("ok")

# формат работы: ремот / гибрид / офис
from main import work_match
job = lambda text, loc="Berlin": {"title": "PM", "location": loc, "text": text}
assert work_match(job("Fully remote team"), "remote") and not work_match(job("Fully remote team"), "hybrid")
assert not work_match(job("Hybrid: 2 days in office, remote team"), "remote")
assert work_match(job("Hybrid: 3 days on-site"), "hybrid") and work_match(job("Hybrid: 3 days on-site"), "both")
assert not work_match(job("On-site in Berlin"), "both") and work_match(job("On-site in Berlin"), "any")
assert not work_match(job("This role is not remote"), "remote")
print("work_match ok")

# телефоны без «+»: нужен контекст tel/phone и код страны из локации
from main import dial_code
assert dial_code("Berlin, Berlin, Germany") == "49" and dial_code("Cork Metropolitan Area") is None
assert extract_contacts("Tel: 030 1234567", "49") == {"phones": ["+49 30 1234567"]}
assert extract_contacts("Tel.: <b>0151 23456789</b>", "49") == {"phones": ["+49 151 23456789"]}
assert extract_contacts("Phone (0)6 12 34 56 78", "31") == {"phones": ["+31 6 12 34 56 78"]}
assert extract_contacts("Call 06 1234 5678", "39") == {"phones": ["+39 06 1234 5678"]}  # Италия: ноль остаётся
assert extract_contacts("Call 030 1234567") == {"phones": ["030 1234567"]}  # страна неизвестна: как написано
assert extract_contacts("Posted 01.02.2025, ref 0123456789", "49") == {}  # без слова tel/phone не телефон
print("local phones ok")

# дубль: та же компания/название/город под другим ID
import os, tempfile, main
from contextlib import closing
main.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
a = {"id": "1", "url": "u1", "company": "ACME GmbH", "title": "Marketing Manager (m/f/d)", "location": "Berlin, Germany"}
b = {**a, "id": "2", "url": "u2", "company": "Acme  GmbH"}
with closing(main.db_conn()) as db:
    assert not main.is_duplicate(db, a)
    main.mark_seen(db, a)
    assert main.is_duplicate(db, b) and not main.is_duplicate(db, {**b, "title": "Sales Manager"})
print("dedupe ok")

from main import reject_reason
assert "нет слов" in reject_reason(job("Teach curriculum, MongoDB"), "both")
assert "гибрид" in reject_reason(job("Hybrid, remote friendly"), "remote")
assert "офисная" in reject_reason(job("remote not possible, on-site only"), "remote")
print("reasons ok")

# мягкий офисный фильтр: слово on-site само по себе не отсекает, явные «не удалённая» — отсекают
assert work_match(job("Fully remote team, on-site visits twice a year"), "remote")
assert not work_match(job("Remote not possible for this role"), "remote")
assert not work_match(job("This is an office-based role, no remote"), "both")
assert not work_match(job("Remote? This position is fully on-site"), "remote")
print("onsite filter ok")

# сборка .env на новой машине: токен и chat id подставляются, остальное берётся из шаблона
from main import build_env, DB_PATH, HERE
tpl = "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nSEARCH_KEYWORDS=a,b\nHEADLESS=true\n"
out = build_env("1:tok", "42", tpl)
assert out.splitlines()[:2] == ["TELEGRAM_BOT_TOKEN=1:tok", "TELEGRAM_CHAT_ID=42"] and "SEARCH_KEYWORDS=a,b" in out
assert out.count("TELEGRAM_BOT_TOKEN=") == 1
assert os.path.dirname(DB_PATH) == str(HERE) or os.getenv("DB_PATH")  # база лежит рядом с main.py, а не в текущей папке
print("env setup ok")
