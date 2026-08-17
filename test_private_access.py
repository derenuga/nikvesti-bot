"""
Тест приватного фільтра бота: кого пускає, кого ні і куди саме.

Навіщо окремий тест. До 17.08.2026 приватний фільтр різав усіх, крім трьох
id у ALLOWED_USER_IDS, — і різав ДО того, як справа доходила до команди. Тобто
Іміра, для якої зроблені й `/carousel`, і апка, на `/team` у приваті бачила
глухе «Доступ заборонено», хоч ростер її пускає. Межа тепер проходить по
ростеру, і в цьому легко перестаратись у зворотний бік: пустити разом із
командами й вільний текст, тобто відкрити NLQ (гроші за модель) усій редакції.
Тест стереже обидва краї одночасно.

Правило 4 з CLAUDE.md (тест не залежить від мережі) тут виконується само:
жодного походу назовні, лише підміна оточення й фальшиві update.

Запуск: python test_private_access.py
"""

import asyncio
import os
import sys
import types


# ---------- фальшивий Telegram ----------

class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeChat:
    def __init__(self, ctype="private"):
        self.type = ctype


class FakeMessage:
    def __init__(self, text=None, caption=None):
        self.text = text
        self.caption = caption
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user, message=None, chat_type="private", callback=False):
        self.effective_user = user
        self.effective_chat = FakeChat(chat_type)
        self.message = message
        self.callback_query = object() if callback else None


def load_bot():
    """Імпортує bot.py з мінімальним оточенням.

    bot.py на імпорті тягне пів проєкту й читає ALLOWED_USER_IDS у множину,
    тому змінну ставимо ДО імпорту, а сам модуль підвантажуємо один раз.
    """
    os.environ["ALLOWED_USER_IDS"] = "56631818,56424866,386403807"
    os.environ.setdefault("BOT_TOKEN", "test:token")
    sys.modules.pop("bot", None)
    import bot
    return bot


async def gate(bot, update):
    """Проганяє update крізь check_allowed.

    Повертає "pass" (фільтр пропустив далі), "stop" (зупинив ланцюг) —
    ApplicationHandlerStop і є способом PTB сказати «далі не йде».
    """
    from telegram.ext import ApplicationHandlerStop
    try:
        await bot.check_allowed(update, None)
        return "pass"
    except ApplicationHandlerStop:
        return "stop"


def main():
    bot = load_bot()
    results = []

    def check(ok, title, detail=""):
        results.append(ok)
        print(f"  {'✅' if ok else '❌'} {title}{'' if ok else f' — {detail}'}")

    oleh = FakeUser(56631818, "derenuga")
    imira = FakeUser(70000001, "Imira_91")      # ростер знає її за username
    liza = FakeUser(386403807, "mskvn1")        # у ALLOWED_USER_IDS
    stranger = FakeUser(99999999, "random_person")

    print("Приватний фільтр бота:")

    # --- хто в ALLOWED_USER_IDS: усе, як було ---
    m = FakeMessage(text="скільки трафіку за тиждень")
    check(asyncio.run(gate(bot, FakeUpdate(oleh, m))) == "pass",
          "Олег питає словами — пускає (NLQ його)")

    m = FakeMessage(text="/team")
    check(asyncio.run(gate(bot, FakeUpdate(liza, m))) == "pass",
          "Ліза з вайтлиста — пускає")

    # --- людина з ростера: команди й кнопки ---
    m = FakeMessage(text="/team")
    r = asyncio.run(gate(bot, FakeUpdate(imira, m)))
    check(r == "pass", "Іміра кличе /team — ПУСКАЄ", f"віддало {r}")

    m = FakeMessage(text="/carousel https://nikvesti.com/322371")
    check(asyncio.run(gate(bot, FakeUpdate(imira, m))) == "pass",
          "Іміра кличе /carousel з лінком — пускає")

    check(asyncio.run(gate(bot, FakeUpdate(imira, None, callback=True))) == "pass",
          "тап по кнопці від Іміри — пускає (продовження команди)")

    # --- людина з ростера: вільний текст лишається за вайтлистом ---
    m = FakeMessage(text="а скільки в нас трафіку?")
    r = asyncio.run(gate(bot, FakeUpdate(imira, m)))
    check(r == "stop", "Іміра пише словами — НЕ пускає (це NLQ за гроші)",
          f"віддало {r}")
    check(m.replies and "«/»" in m.replies[0],
          "і сказано, що робити: команди тут, питання — реплаєм у чаті",
          f"відповідь: {m.replies}")
    check(m.replies and "заборонено" not in m.replies[0].lower(),
          "своїй людині не кажемо «доступ заборонено»")

    # --- чужа людина: як і було ---
    m = FakeMessage(text="/team")
    r = asyncio.run(gate(bot, FakeUpdate(stranger, m)))
    check(r == "stop", "чужа людина з командою — стоп", f"віддало {r}")
    check(m.replies and "заборонено" in m.replies[0].lower(),
          "і бачить рівно те саме, що бачила")

    check(asyncio.run(gate(bot, FakeUpdate(stranger, None, callback=True))) == "stop",
          "чужий тап по кнопці — теж стоп")

    # --- група не зачіпається взагалі ---
    m = FakeMessage(text="будь-що")
    check(asyncio.run(gate(bot, FakeUpdate(stranger, m, chat_type="group"))) == "pass",
          "у групі фільтр мовчить (там свої перевірки)")

    bad = len([r for r in results if not r])
    print(f"\n{'❌ Провалено: %d із %d' % (bad, len(results)) if bad else '✅ Усі %d перевірки пройшли' % len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
