"""Проверка поиска кандидатов (/find) без сети: запросы, разбор выдачи, диалог."""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")  # не трогаем рабочую базу
import main
from main import build_queries, format_profile, handle, name_from_slug, parse_profiles, wizard_answer

# запросы: по одному на вариант должности, условия в кавычках, уровень одним словом
c = {"role": "sound engineer, AV engineer", "country": "Azerbaijan", "city": "Baku",
     "education": "bachelor, Computer Engineering", "experience": "Senior", "skills": "Dante"}
assert build_queries(c) == [
    'site:linkedin.com/in "sound engineer" "Baku" "Azerbaijan" "bachelor" "Computer Engineering" senior "Dante"',
    'site:linkedin.com/in "AV engineer" "Baku" "Azerbaijan" "bachelor" "Computer Engineering" senior "Dante"']
assert build_queries({"role": "designer", "experience": "5 years"}) == ['site:linkedin.com/in "designer" "5 years"']
assert build_queries({"role": 'say "hi"'}) == ['site:linkedin.com/in "say hi"']
print("queries ok")

# разбор выдачи: редирект DuckDuckGo, поддомен страны, повторы, чужие ссылки
anchors = [
    ["//duckduckgo.com/l/?uddg=https%3A%2F%2Faz.linkedin.com%2Fin%2Fjohn%2Ddoe%2D1a2b3c&rut=x",
     "John Doe - Sound Engineer - Acme | LinkedIn", "result__a", ""],
    ["//duckduckgo.com/l/?uddg=https%3A%2F%2Faz.linkedin.com%2Fin%2Fjohn%2Ddoe%2D1a2b3c&rut=x",
     "az.linkedin.com/in/john-doe-1a2b3c", "result__url", ""],
    ["//duckduckgo.com/l/?uddg=https%3A%2F%2Faz.linkedin.com%2Fin%2Fjohn%2Ddoe%2D1a2b3c&rut=x",
     "Baku, Azerbaijan · Sound Engineer · Acme", "result__snippet", ""],
    ["https://www.linkedin.com/in/Jane-Roe?trk=x", "Jane Roe – AV Engineer", "",
     "Jane Roe – AV Engineer\nwww.linkedin.com/in/jane-roe\nBaku. Education: Bachelor"],
    ["https://www.linkedin.com/company/acme", "Acme | LinkedIn", "result__a", ""],
    ["https://www.linkedin.com/jobs/view/1/", "Job", "result__a", ""],
    ["https://example.com/in/x", "Other", "", ""],
    [None, "", "", ""],
]
got = parse_profiles(anchors)
assert got == [
    {"slug": "john-doe-1a2b3c", "url": "https://www.linkedin.com/in/john-doe-1a2b3c",
     "title": "John Doe - Sound Engineer - Acme", "snippet": "Baku, Azerbaijan · Sound Engineer · Acme"},
    {"slug": "jane-roe", "url": "https://www.linkedin.com/in/Jane-Roe",
     "title": "Jane Roe – AV Engineer", "snippet": "Baku. Education: Bachelor"}], got
assert name_from_slug("ruslan-aliyev-1a2b3c") == "Ruslan Aliyev"
card = format_profile({**got[0], "title": "A <b> & C"})
assert "A &lt;b&gt; &amp; C" in card and 'href="https://www.linkedin.com/in/john-doe-1a2b3c"' in card
print("parse ok")

# диалог /find: вопросы по очереди, пропуски, проверка числа, запуск и повтор прошлого поиска
started = []
main.start_candidates = started.append
text, buttons = handle("/find")
assert text.startswith("1/7") and buttons == []
assert wizard_answer("  ")[0].startswith("1/7")  # должность обязательна
assert wizard_answer("sound engineer")[0].startswith("2/7")
assert wizard_answer("Azerbaijan")[0].startswith("3/7")
assert wizard_answer("Пропустить")[0].startswith("4/7")
text, buttons = wizard_answer("bachelor")
assert text.startswith("5/7") and buttons == ["Любой", "junior", "middle", "senior", "lead"]
assert wizard_answer("Любой")[0].startswith("6/7")
assert wizard_answer("Пропустить")[0].startswith("7/7")
assert wizard_answer("много")[0].startswith("Напишите число")
text, buttons = wizard_answer("20")
assert 'site:linkedin.com/in "sound engineer" "Azerbaijan" "bachelor"' in text and buttons == ["Искать", "Отмена"]
assert wizard_answer("что?")[1] == ["Искать", "Отмена"] and not started
assert wizard_answer("Искать")[0].startswith("Ищу") and main.wizard is None
assert started == [{"role": "sound engineer", "country": "Azerbaijan", "city": "", "education": "bachelor",
                    "experience": "", "skills": "", "limit": "20"}]

text, buttons = handle("/find")
assert buttons == ["Повторить прошлый поиск"]
assert wizard_answer("Повторить прошлый поиск")[1] == ["Искать", "Отмена"]
assert wizard_answer("Отмена") == ("Отменил.", []) and main.wizard is None and len(started) == 1
handle("/find")
assert handle("/cancel") == ("Отменил.", []) and main.wizard is None
assert handle("/cancel").startswith("Вопросов сейчас нет")
print("wizard ok")

# запасной поиск Gemini: берём только источники, которые ведут на профиль
class FakeResp:
    def __init__(self, data=None, headers=None):
        self._data, self.headers = data, headers or {}
    def raise_for_status(self):
        pass
    def json(self):
        return self._data

main.requests.post = lambda *a, **k: FakeResp({"candidates": [{"groundingMetadata": {
    "groundingChunks": [{"web": {"uri": "https://redirect.example/1", "title": "linkedin.com"}},
                        {"web": {"uri": "https://redirect.example/2", "title": "example.com"}}],
    "groundingSupports": [{"segment": {"text": "John Doe, Sound Engineer, Baku."}, "groundingChunkIndices": [0]},
                          {"segment": {"text": "Not a profile."}, "groundingChunkIndices": [1]}]}}]})
main.requests.get = lambda uri, **k: FakeResp(headers={
    "Location": "https://www.linkedin.com/in/john-doe-1a2b3c" if uri.endswith("/1") else "https://example.com/x"})
assert main.gemini_search("q") == [{"slug": "john-doe-1a2b3c", "url": "https://www.linkedin.com/in/john-doe-1a2b3c",
                                    "title": "John Doe", "snippet": "John Doe, Sound Engineer, Baku.", "ai": True}]
print("candidates ok")
