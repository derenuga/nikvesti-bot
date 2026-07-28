"""
Тест кнопки «Дедлайни в апці» під нагадуваннями про звіти.

Навіщо окремий тест: кнопка вже раз не з'явилась у чаті фінансів через те,
що покладалась на змінну WEBAPP_DIRECT_LINK, якої ніхто не ставив. Тепер лінк
збирається сам із username бота, і тест стереже саме це — щоб повернення до
«спочатку налаштуй змінну» не проїхало непоміченим.

Запуск: python test_report_link.py
"""

import asyncio
import importlib
import os
import sys


class FakeMe:
    def __init__(self, username):
        self.username = username


class FakeBot:
    """Мінімальний бот: рахує виклики get_me, щоб перевірити кешування."""

    def __init__(self, username="mykvisti_bot"):
        self._username = username
        self.get_me_calls = 0

    async def get_me(self):
        self.get_me_calls += 1
        if self._username is None:
            raise RuntimeError("Unauthorized")
        return FakeMe(self._username)


def load(direct_link=None):
    """Свіжий модуль із заданим оточенням: WEBAPP_DIRECT_LINK читається на
    імпорті, тому без перезавантаження тести бачили б стан попереднього."""
    if direct_link is None:
        os.environ.pop("WEBAPP_DIRECT_LINK", None)
    else:
        os.environ["WEBAPP_DIRECT_LINK"] = direct_link
    sys.modules.pop("handlers.report_reminders", None)
    # кеш лінка живе в helpers і переживає перезавантаження модуля
    from handlers import helpers
    helpers._app_link_cache.clear()
    return importlib.import_module("handlers.report_reminders")


def button_url(markup):
    return markup.inline_keyboard[0][0].url


async def main():
    failures = []

    def check(label, cond, detail=""):
        print(f"{'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    # 1. Без змінної лінк збирається з username — головний сценарій прода
    rr = load(None)
    bot = FakeBot()
    url, source = await rr.app_link(bot)
    check("без WEBAPP_DIRECT_LINK лінк береться з username",
          url == "https://t.me/mykvisti_bot" and source == "get_me", f"{url} / {source}")
    markup = rr._app_markup(url)
    check("кнопка веде одразу в «Звітність»",
          button_url(markup) == "https://t.me/mykvisti_bot?startapp=reports",
          button_url(markup))

    # 2. get_me смикається один раз на процес
    await rr.app_link(bot)
    await rr.app_link(bot)
    check("get_me кешується", bot.get_me_calls == 1, f"викликів: {bot.get_me_calls}")

    # 3. Явна змінна перебиває автовизначення (форма з short name)
    rr = load("https://t.me/mykvisti_bot/team")
    bot = FakeBot()
    url, source = await rr.app_link(bot)
    check("WEBAPP_DIRECT_LINK має пріоритет",
          url == "https://t.me/mykvisti_bot/team" and source == "env", f"{url} / {source}")
    check("з заданим лінком get_me не потрібен", bot.get_me_calls == 0)
    check("startapp додається через ?",
          button_url(rr._app_markup(url)) == "https://t.me/mykvisti_bot/team?startapp=reports",
          button_url(rr._app_markup(url)))

    # 4. Лінк уже з параметрами — startapp дочіпляється через &
    rr = load("https://t.me/mykvisti_bot/team?mode=x")
    url, _ = await rr.app_link(FakeBot())
    check("параметри в лінку не ламають startapp",
          button_url(rr._app_markup(url)).endswith("?mode=x&startapp=reports"),
          button_url(rr._app_markup(url)))

    # 5. get_me впав — повідомлення має піти без кнопки, а не впасти
    rr = load(None)
    url, source = await rr.app_link(FakeBot(username=None))
    check("збій get_me не валить нагадування", url is None and rr._app_markup(url) is None)

    print()
    if failures:
        print(f"Провалено: {len(failures)}")
        for f in failures:
            print(f"  • {f}")
        return 1
    print("Усі перевірки пройдені")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
