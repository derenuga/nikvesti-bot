"""
Зарахування виконання creative tasks — судья-матчер (концепт, крок 1: ТЕСТ).

Проблема (Олег, 27.07): БД сайту віддає, що публікація зроблена В РАМКАХ
проєкту (nodes.partner_project), але НЕ каже, по якій тематиці. А таска
привʼязана саме до тематики. Тобто по (owner_id + partner_project + type)
коло звужується, але яка з тематик проєкту виконана — зі структурних даних
не видно. Синхронізація тематики в БД сайту буде колись (недоступний
програміст) — поки закриваємо це AI-суддею.

Пайплайн (на публікацію, що вийшла в проєкті з відкритими тасками):
  1. Дешевий пре-фільтр БЕЗ AI: кандидати — відкриті таски цієї людини
     (owner_id→person) у цьому проєкті, сумісні за типом (тип таска = тип
     ноди, або таска «будь-який»).
  2. Немає кандидатів → нічого не робимо.
  3. Суддя (Claude, tool use → строгий вердикт): читає заголовок + лід +
     рубрику статті і перелік тематик-кандидатів → обирає ОДНУ тематику, яку
     публікація виконує, або «жодна», з рівнем впевненості.
  4. high  → авто-зарахування; medium → черга на рішення Каті; low/жодна →
     тихо (лог «бачено, не збіг»), нікого не смикаємо.

Цей файл поки — ТІЛЬКИ ТЕСТ: /match_test <url> проганяє суддю на реальній
статті проти реальних відкритих тасків автора і показує вердикт. НІЧОГО НЕ
ЗАРАХОВУЄ і не пише — щоб перевірити якість матчингу, перш ніж вмикати
авто-облік.
"""

import os
import re

from handlers import db, team_projects, team_roster, team_tasks
from handlers.ai_messages import async_client, _record_usage
from handlers.helpers import extract_article_id
from handlers.team_projects import _norm_name

ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

JUDGE_MODEL = "claude-sonnet-5"  # матчинг — відповідальна задача, беремо SMART

CONFIDENCE_ACTION = {
    "high": "авто-зарахування",
    "medium": "на рішення Каті",
    "low": "не зараховувати",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html, limit=1600):
    text = _TAG_RE.sub(" ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def load_node_signal(node_id):
    """Сигнатура публікації з БД сайту для судді: заголовок, лід (початок
    тексту), рубрика, автор (owner_id), проєкт (partner_project), тип.
    Повертає dict або None (немає ноди / БД недоступна)."""
    if not db.is_configured():
        return None
    rows = db.query(
        "SELECT id, type, category, status, owner_id, partner_project, "
        "COALESCE(title_ua, title) AS title, "
        "COALESCE(content_ua, content) AS content "
        "FROM nodes WHERE id = %s",
        (int(node_id),),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "node_id": r["id"],
        "type": r["type"],
        "category": r["category"],
        "status": r["status"],
        "owner_id": r["owner_id"],
        "partner_project": r["partner_project"],
        "title": (r["title"] or "").strip(),
        "lead": _strip_html(r["content"]),
    }


def resolve_owner_person(owner_id):
    """owner_id (users.id) → канонічне імʼя ростера, або None."""
    if not owner_id or not db.is_configured():
        return None
    rows = db.query(
        "SELECT first_name, last_name FROM users WHERE id = %s", (int(owner_id),)
    )
    if not rows:
        return None
    full = _norm_name(f"{rows[0]['first_name'] or ''} {rows[0]['last_name'] or ''}")
    for name in team_roster.ROSTER:
        if _norm_name(name) == full:
            return name
    return None


def candidate_tasks(person, project_id, node_type):
    """Пре-фільтр БЕЗ AI: відкриті таски людини в цьому проєкті, сумісні за
    типом (тип таска збігається з типом ноди, або таска «будь-який»).
    Пости в облік не беремо (їх у nodes немає — окрема історія)."""
    if not person or not project_id:
        return []
    out = []
    for t in team_tasks.list_tasks(person):
        if t["status"] != "open":
            continue
        if t["project_id"] != int(project_id):
            continue
        if t["type"] == "post":
            continue
        if t["type"] and t["type"] != node_type:
            continue
        out.append(t)
    return out


_JUDGE_TOOL = {
    "name": "verdict",
    "description": "Вердикт: яку тематику-таску виконує ця публікація (або жодну).",
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_task_id": {
                "type": ["integer", "null"],
                "description": "id таски, яку публікація виконує; null — жодна з наведених",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high — публікація чітко і конкретно виконує саме цю тематику; "
                               "medium — схоже, але є сумнів; low — слабкий звʼязок",
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 речення українською, чому саме ця тематика (або чому жодна) — для редактора",
            },
        },
        "required": ["matched_task_id", "confidence", "reasoning"],
    },
}


async def judge_publication(article, candidates):
    """AI-суддя: обирає тематику-кандидата, яку виконує публікація, або жодну.
    article — {title, lead, category, type}; candidates — list таск-dict.
    Повертає {matched_task_id, confidence, reasoning, task} (task — обрана або None)."""
    if not candidates:
        return {"matched_task_id": None, "confidence": "low",
                "reasoning": "У автора немає відкритих тасків у цьому проєкті.", "task": None}

    lines = []
    for t in candidates:
        theme = t["theme_name"] or "(без тематики)"
        type_word = {"news": "новина", "article": "стаття"}.get(t["type"], "будь-який тип")
        lines.append(f"- id={t['id']}: тематика «{theme}» ({type_word})"
                     + (f", нотатка: {t['note']}" if t["note"] else ""))
    tasks_block = "\n".join(lines)

    prompt = (
        "Ти — прискіпливий редактор, що звіряє вихід публікації із планом завдань.\n\n"
        f"ПУБЛІКАЦІЯ (тип: {article['type']}, рубрика: {article.get('category') or '—'}):\n"
        f"Заголовок: {article['title']}\n"
        f"Початок тексту: {article['lead']}\n\n"
        f"ВІДКРИТІ ЗАВДАННЯ АВТОРА в цьому проєкті (обери ОДНЕ, яке ця публікація виконує):\n"
        f"{tasks_block}\n\n"
        "Правила:\n"
        "• Обирай тематику, ЗМІСТ якої публікація конкретно виконує (не просто «той самий проєкт»).\n"
        "• confidence=high лише коли публікація явно і однозначно про цю тематику.\n"
        "• Якщо жодна тематика не підходить за змістом — matched_task_id=null.\n"
        "• Якщо публікація рівною мірою пасує двом — обери найточнішу і став medium.\n"
        "Виклич інструмент verdict."
    )

    msg = await async_client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=400,
        thinking={"type": "disabled"},
        tools=[_JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "verdict"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        _record_usage(JUDGE_MODEL, msg.usage)
    except Exception:
        pass
    verdict = next((b.input for b in msg.content if b.type == "tool_use"), None) or {
        "matched_task_id": None, "confidence": "low", "reasoning": "Суддя не повернув вердикт."}
    tid = verdict.get("matched_task_id")
    verdict["task"] = next((t for t in candidates if t["id"] == tid), None)
    if tid is not None and verdict["task"] is None:
        # Суддя назвав id поза списком — вважаємо «жодна»
        verdict["matched_task_id"] = None
    return verdict


# ---------- Тестова команда (нічого не зараховує) ----------

async def match_test_handler(update, context):
    """/match_test <url> — прогнати суддю на реальній статті проти реальних
    відкритих тасків її автора. Показує вердикт і що БУЛО Б зроблено —
    але НІЧОГО не зараховує. Діагностика якості матчингу перед авто-обліком."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /match_test <url статті nikvesti.com>")
        return
    url = context.args[0]
    node_id = extract_article_id(url)
    if not node_id:
        await update.message.reply_text("Не змогла дістати id матеріалу з URL.")
        return

    msg = await update.message.reply_text("🦊 Дивлюсь публікацію і звіряю з планом…")

    import asyncio
    signal = await asyncio.to_thread(load_node_signal, node_id)
    if not signal:
        await msg.edit_text(f"Ноду {node_id} не знайдено в БД сайту (або БД недоступна).")
        return

    person = await asyncio.to_thread(resolve_owner_person, signal["owner_id"])
    project_id = signal["partner_project"]
    project = None
    if project_id:
        projects = await asyncio.to_thread(team_projects.list_projects, False)
        project = next((p for p in projects if p["id"] == int(project_id)), None)

    header = (
        f"📄 <b>{signal['title'][:120]}</b>\n"
        f"Тип: {signal['type']} · рубрика: {signal.get('category') or '—'}\n"
        f"Автор (owner_id {signal['owner_id']}): {person or '❓ не в ростері'}\n"
        f"Проєкт (partner_project {project_id or '—'}): "
        f"{(project['partner'] + ' · ' + project['name']) if project else '❓ поза проєктом'}\n"
    )
    if not person:
        await msg.edit_text(header + "\n⚠️ Автора не зіставлено з ростером — матчинг неможливий.", parse_mode="HTML")
        return
    if not project_id:
        await msg.edit_text(header + "\n⚠️ Публікація не в рамках проєкту — таски не звіряються.", parse_mode="HTML")
        return

    candidates = await asyncio.to_thread(candidate_tasks, person, project_id, signal["type"])
    if not candidates:
        await msg.edit_text(
            header + f"\n📋 Відкритих тасків {person} у цьому проєкті (сумісних за типом) — немає.\n"
            "Нічого зараховувати.", parse_mode="HTML")
        return

    cand_lines = "\n".join(
        f"  • id={t['id']}: «{t['theme_name'] or '(без тематики)'}» ({t['qty']} × {t['type'] or 'будь-який'})"
        for t in candidates)

    verdict = await judge_publication(
        {"title": signal["title"], "lead": signal["lead"],
         "category": signal["category"], "type": signal["type"]},
        candidates,
    )

    conf = verdict["confidence"]
    action = CONFIDENCE_ACTION.get(conf, "?")
    icon = {"high": "✅", "medium": "🤔", "low": "🚫"}.get(conf, "•")
    if verdict["task"]:
        result = (f"{icon} <b>Збіг:</b> «{verdict['task']['theme_name'] or '(без тематики)'}» "
                  f"(id={verdict['task']['id']})\n"
                  f"Впевненість: <b>{conf}</b> → {action}")
    else:
        result = f"{icon} <b>Жодна тематика не підходить</b>\nВпевненість: {conf} → нічого не зараховувати"

    await msg.edit_text(
        f"{header}\n📋 Кандидати ({len(candidates)}):\n{cand_lines}\n\n"
        f"{result}\n💬 {verdict['reasoning']}\n\n"
        f"<i>Тест — нічого не зараховано.</i>",
        parse_mode="HTML", disable_web_page_preview=True,
    )
