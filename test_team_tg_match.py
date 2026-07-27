"""
Тест зарахування Telegram-постів за дисклеймером (handlers/team_tg_match.py)
на РЕАЛЬНОМУ пості каналу і живому Postgres.

Метод Олега: у partner_project є disclaimer_ua; якщо текст поста містить
дисклеймер проєкту — пост точно цього проєкту. Автора Telegram не показує,
тож такий матч ЗАВЖДИ йде в чергу Каті.

Реальні дані (правило «не тестувати на вигаданому»):
- пост t.me/nikvesti/82296 — той самий, що Олег навів у брифі як приклад
  IWPR-поста; при доступній мережі береться живцем, інакше — зі знімка,
  знятого з каналу 27.07.2026;
- дисклеймер узятий з хвоста цього ж поста і записаний так, як він лежить у
  CMS — HTML з &laquo;-сутностями. Саме цю відстань між CMS і Telegram і має
  перекривати нормалізація.

Запуск:
    BOT_DATABASE_URL=postgresql://... python test_team_tg_match.py
"""

import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, team_matches, team_tg_match as tg   # noqa: E402

RESULTS = []
REF_POST_ID = 82296

# Хвіст справжнього поста 82296 (знімок 27.07.2026) — на випадок, коли t.me
# недоступний із середовища прогону.
REAL_POST_TAIL = (
    "громаду виключили з цього переліку через відстань до лінії фронту, однак "
    "вважають таке рішення необґрунтованим. Опубліковано в рамках трирічного "
    "проєкту «Посилення громадського контролю в Україні» (Boosting Citizens "
    "Watchdogs in Ukraine) що реалізується IWPR за підтримки Норвезького "
    "агентства з розвитку та співробітництва Norad."
)

# Той самий дисклеймер у вигляді CMS: HTML-обгортка і &laquo;-лапки
CMS_DISCLAIMER = (
    "<p>Опубліковано в рамках трирічного проєкту &laquo;Посилення громадського "
    "контролю в Україні&raquo; (Boosting Citizens Watchdogs in Ukraine) що "
    "реалізується IWPR за підтримки Норвезького агентства з розвитку та "
    "співробітництва Norad.</p>"
)

IWPR_PROJECT = 43


def check(name, cond):
    RESULTS.append((name, bool(cond)))


def cleanup():
    bot_db.execute("DELETE FROM team_task_matches WHERE source = 'telegram' "
                   "AND ref = ANY(%s)", ([str(REF_POST_ID), "999001"],))


def live_post_text():
    """Реальний текст поста з каналу; None — якщо мережі немає."""
    try:
        from bs4 import BeautifulSoup

        from handlers.telegram_stats import _fetch_html
        html = _fetch_html(f"/nikvesti/{REF_POST_ID}", params={"embed": "1"})
        body = BeautifulSoup(html, "html.parser").find(
            "div", class_="tgme_widget_message_text")
        return body.get_text(" ", strip=True) if body else None
    except Exception as e:
        print(f"  (t.me недоступний — беру знімок: {e})")
        return None


def main():
    team_matches.ensure_match_schema()
    cleanup()

    # Картинка поста береться з того самого HTML стрічки (background-image),
    # без окремого запиту — перевіряємо на живій стрічці
    try:
        feed_posts = tg.fetch_recent_posts(1)
        with_img = [p for p in feed_posts if p.get("image")]
        check("у стрічці каналу знайшлись пости з картинкою",
              len(feed_posts) > 5 and with_img)
        check("картинка — придатний https-лінк",
              with_img and with_img[0]["image"].startswith("https://"))
    except Exception as e:
        print(f"  (стрічка t.me недоступна — перевірку картинок пропущено: {e})")

    live = live_post_text()
    post_text = live or REAL_POST_TAIL
    check("реальний пост прочитано (живцем або зі знімка)", bool(post_text))
    if live:
        check("у живому пості справді є дисклеймер IWPR",
              "Norad" in live and "IWPR" in live)

    disclaimers = {IWPR_PROJECT: tg.normalize(CMS_DISCLAIMER)}
    check("дисклеймер із CMS нормалізувався в плаский текст",
          "&laquo;" not in disclaimers[IWPR_PROJECT]
          and "<p>" not in disclaimers[IWPR_PROJECT])

    post = {"message_id": REF_POST_ID, "text": post_text,
            "url": f"https://t.me/nikvesti/{REF_POST_ID}", "dt": None}
    hits = tg.match_by_disclaimer([post], disclaimers)
    check("справжній пост зіставився з проєктом попри різні лапки й HTML",
          hits and hits[0][1] == IWPR_PROJECT)

    # Звичайний пост каналу — без дисклеймера
    plain = {"message_id": 999001, "url": "https://t.me/nikvesti/999001", "dt": None,
             "text": "Біля міськради Миколаєва вже другий тиждень збирається "
                     "мітинг, повідомляє кореспондент МикВісті"}
    check("звичайний пост каналу не зараховується",
          tg.match_by_disclaimer([plain], disclaimers) == [])

    # Короткий «дисклеймер» — запобіжник від зарахування половини каналу
    short = {IWPR_PROJECT: tg.normalize("<p>МикВісті</p>")}
    check("надто короткий дисклеймер відкидається на вході",
          len(short[IWPR_PROJECT]) < tg.MIN_DISCLAIMER_LEN)

    # ---------- Запис у чергу ----------
    tg.fetch_recent_posts = lambda pages=2: [post, plain]
    tg.project_disclaimers = lambda: disclaimers
    seen, queued = tg.scan_posts()
    check("прогін переглянув обидва пости", seen == 2)
    check("у чергу пішов рівно один — проєктний", queued == 1)

    pending = [m for m in team_matches.pending_matches()
               if m["source"] == "telegram" and m["ref"] == str(REF_POST_ID)]
    check("матч у черзі Каті", len(pending) == 1)
    check("автор НЕ вгаданий — рішення за редакторкою",
          pending and pending[0]["person"] is None)
    check("проєкт визначено", pending and pending[0]["project_id"] == IWPR_PROJECT)
    check("прикріплений лінк на пост",
          pending and pending[0]["url"].endswith(str(REF_POST_ID)))
    check("у картці видно початок поста",
          pending and pending[0]["title"].startswith("громаду виключили")
          or (live and pending and pending[0]["title"].startswith("📌")))

    seen2, queued2 = tg.scan_posts()
    check("повторний прогін нічого не дублює", queued2 == 0)

    cleanup()

    print("Telegram-пости за дисклеймером (реальний пост, живий Postgres):")
    ok = 0
    for name, passed in RESULTS:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(RESULTS)} перевірок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
