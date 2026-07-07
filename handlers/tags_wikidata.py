"""
Прив'язка тегів сайту до сутностей Wikidata (Q-entities) для schema.org-розмітки.

Мета: у структурній розмітці сторінки новини виводити секцію `about` з
машиночитаними сигналами для Google:

    "about": [
      {"@type": "Thing", "name": "Російсько-українська війна",
       "sameAs": "https://www.wikidata.org/wiki/Q83085"},
      {"@type": "Place", "name": "Одеська область",
       "sameAs": "https://www.wikidata.org/wiki/Q13363"}
    ]

Для цього до таблиці `tag` треба додати колонку `wikidata_qid` (+ `wikidata_type`
для @type) і наповнити її. Тег «Війна» → Q83085, «Одещина» → Q13363 тощо.

ЧОМУ ЦЕ РОБИТЬ БОТ, А НЕ Я ЛОКАЛЬНО:
- БД сайту (MySQL) закрита whitelist'ом по вихідних IP Railway — прочитати теги
  можна лише зсередини бота;
- Wikidata API з середовища розробки часто недоступний (egress-політика), а з
  Railway — вільно;
- Anthropic API вже налаштований у боті.
Тобто єдина точка, де сходяться всі три доступи (MySQL + Wikidata + Claude) —
це сам бот на Railway.

ЧОМУ ВИХІД — SQL, А НЕ ПРЯМИЙ ЗАПИС: користувач `nikvesti_bot` має ТІЛЬКИ SELECT
(0 UPDATE, це production-БД сайту). Колонку фізично записати неможливо — тому
бот віддає готовий `ALTER` + `UPDATE`, який редакція застосовує в PHPMyAdmin.

Механізм зіставлення (на прогін, у фоні — як /archive_backfill):
1. топ-N канонічних тегів за реальним ужитком (node_tag, з розмерджуванням
   redirect_tag_id у канонічний — та сама логіка, що в archive_mirror);
2. для кожного — пошук у Wikidata (wbsearchentities) укр- і рос-назвою,
   кандидати з мітками й описами;
3. Claude семантично добирає найкращий QID СУВОРО з-поміж кандидатів (щоб не
   вигадати неіснуючий Q-номер) + тип schema.org (Thing/Place/Person/…), або
   null, якщо жоден не пасує — краще без лінка, ніж хибний сигнал для Google;
4. на виході два файли в Telegram: CSV на ревʼю (з кандидатами й впевненістю)
   та .sql (ALTER + UPDATE; невпевнені — закоментовані, щоб нічого хибного не
   застосувалось автоматично).

Команди:
  /tags_export [N]   — топ-N тегів за ужитком у CSV (за замовч. 300), без Wikidata
  /tags_wiki   [N]   — повний прогін зіставлення (за замовч. 100), два файли
"""

import asyncio
import csv
import io
import json
import os
import re
import time
from datetime import datetime

import requests

from handlers import db
from handlers.ai_messages import FOX_MODEL_SMART, async_client, _record_usage

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Wikidata вимагає змістовний User-Agent (інакше 403/429). Контакт — редакції.
WIKIDATA_UA = "NikvestiDeskBot/1.0 (https://nikvesti.com; derenuga@gmail.com)"

# Кандидатів на мову пошуку. Більше — краща повнота, але довший промпт Claude.
WIKI_LIMIT = 6
# Пауза між зверненнями до Wikidata — ввічливість до публічного API.
WIKI_PAUSE = 0.15
# Тегів на один виклик Claude. 12 × (тег + ~12 кандидатів) вкладається у вікно
# й лишає запас на JSON-відповідь.
BATCH = 12
# Нижче цієї впевненості UPDATE у .sql іде ЗАКОМЕНТОВАНИМ (ручне ревʼю).
CONF_APPLY = 0.7

# Дозволені типи schema.org для @type. Claude обирає з цього списку; усе інше
# зводимо до Thing (найзагальніший — завжди валідний для `about`).
SCHEMA_TYPES = {
    "Thing", "Place", "Person", "Organization",
    "Event", "CreativeWork", "Product",
}

_QID_RE = re.compile(r"^Q[1-9][0-9]*$")

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

_running = {"flag": False}


# ---------- Топ тегів за ужитком (з розмерджуванням redirect) ----------

def _fetch_top_tags(n):
    """Топ-N канонічних тегів за реальним ужитком.

    redirect_tag_id зводимо ланцюгом до канонічного (як у archive_mirror), а
    ужиток мерджених тегів додаємо до канонічного — інакше «Миколаїв» і його
    старий дубль рахувались би окремо й обидва потрапляли б у топ.
    Повертає list[dict] з id/name/name_ru/name_en/google_category/description/usage."""
    tags = db.query(
        "SELECT id, redirect_tag_id, name, name_ru, name_en, "
        "google_category, description FROM tag"
    )
    counts = db.query("SELECT tag_id, COUNT(*) AS c FROM node_tag GROUP BY tag_id")

    redirect = {t["id"]: t.get("redirect_tag_id") for t in tags}
    by_id = {t["id"]: t for t in tags}

    def canon(tid):
        seen = set()
        while redirect.get(tid) and tid not in seen:  # 0 і None однаково falsy
            seen.add(tid)
            tid = redirect[tid]
        return tid

    agg = {}
    for row in counts:
        cid = canon(row["tag_id"])
        agg[cid] = agg.get(cid, 0) + int(row["c"] or 0)

    ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    result = []
    for cid, usage in ranked[:n]:
        t = by_id.get(cid)
        if not t:
            continue
        result.append({
            "id": cid,
            "name": (t.get("name") or "").strip(),
            "name_ru": (t.get("name_ru") or "").strip(),
            "name_en": (t.get("name_en") or "").strip(),
            "google_category": (t.get("google_category") or "").strip(),
            "description": (t.get("description") or "").strip(),
            "usage": usage,
        })
    return result


# ---------- Wikidata: пошук кандидатів ----------

def _wikidata_search(term, lang):
    """wbsearchentities: кандидати-items за назвою в заданій мові.
    [] при помилці/порожньому — прогін не має падати через один тег."""
    term = (term or "").strip()
    if not term:
        return []
    try:
        r = requests.get(
            WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": term,
                "language": lang,
                "uselang": lang,
                "type": "item",
                "limit": WIKI_LIMIT,
                "format": "json",
            },
            headers={"User-Agent": WIKIDATA_UA},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"tags_wiki: Wikidata пошук '{term}' ({lang}) не вдався — {e}")
        return []
    out = []
    for x in data.get("search", []):
        out.append({
            "id": x.get("id"),
            "label": (x.get("label") or "").strip(),
            "description": (x.get("description") or "").strip(),
        })
    return out


def _candidates_for(tag):
    """Об'єднані кандидати за укр- і рос-назвою тегу (дедуп по QID, порядок
    збережено — спершу укр). Синхронно; викликати через to_thread."""
    seen, merged = set(), []
    terms = []
    if tag["name"]:
        terms.append((tag["name"], "uk"))
    if tag["name_ru"] and tag["name_ru"] != tag["name"]:
        terms.append((tag["name_ru"], "ru"))
    for term, lang in terms:
        for c in _wikidata_search(term, lang):
            qid = c["id"]
            if not qid or qid in seen:
                continue
            seen.add(qid)
            merged.append(c)
        # Пауза після кожного HTTP — ввічливість до Wikidata.
        time.sleep(WIKI_PAUSE)
    return merged


# ---------- Claude: семантичний вибір сутності ----------

def _build_prompt(batch):
    """Промпт для одного батчу: теги + їхні кандидати Wikidata. batch — list
    елементів {"tag": tag_dict, "candidates": [...]}."""
    blocks = []
    for item in batch:
        t = item["tag"]
        meta = []
        if t["name_ru"] and t["name_ru"] != t["name"]:
            meta.append(f"рос: {t['name_ru']}")
        if t["name_en"]:
            meta.append(f"англ: {t['name_en']}")
        if t["google_category"]:
            meta.append(f"рубрика: {t['google_category']}")
        if t["description"]:
            meta.append(f"опис: {t['description']}")
        meta_str = f" ({'; '.join(meta)})" if meta else ""
        lines = [f'[T{t["id"]}] "{t["name"]}"{meta_str}']
        if item["candidates"]:
            for c in item["candidates"]:
                desc = f" — {c['description']}" if c["description"] else ""
                lines.append(f'   {c["id"]} — {c["label"]}{desc}')
        else:
            lines.append("   (кандидатів Wikidata немає)")
        blocks.append("\n".join(lines))

    tags_block = "\n\n".join(blocks)
    return f"""Ти зіставляєш теги новинного медіа з Миколаєва (Україна) із сутностями Wikidata для schema.org-розмітки `about` (сигнал для Google про те, ПРО ЩО матеріал).

Нижче теги. Під кожним — кандидати Wikidata (QID — мітка — опис), знайдені пошуком за назвою тегу. Для КОЖНОГО тегу обери НАЙКРАЩИЙ за смислом QID.

СУВОРІ ПРАВИЛА:
- QID обирай ТІЛЬКИ з-поміж перелічених кандидатів цього тегу. НЕ вигадуй Q-номери з памʼяті.
- Якщо жоден кандидат не відповідає тегу за смислом (або кандидатів немає) — постав "qid": null. Краще без лінка, ніж хибний сигнал.
- Розрізняй за контекстом: місто vs область vs район vs футбольний клуб; персона vs однофамілець; загальне поняття vs конкретна подія. Контекст — регіональне українське медіа (Миколаїв, Україна, 2008–2026).
- Тип schema.org обери один з: Thing, Place, Person, Organization, Event, CreativeWork, Product. Для географії — Place; людина — Person; установа/компанія/партія — Organization; подія (війна, вибори, ДТП як явище) — Event або Thing; загальне поняття — Thing.
- confidence — 0.0..1.0, наскільки впевнений у виборі.

Формат відповіді — ТІЛЬКИ JSON-масив, без пояснень поза ним. По одному об'єкту на КОЖЕН тег зі списку, у тому ж порядку:
[{{"tag_id": 123, "qid": "Q13363", "type": "Place", "label": "Одеська область", "confidence": 0.95, "reason": "коротко чому"}}, {{"tag_id": 124, "qid": null, "type": null, "label": null, "confidence": 0.0, "reason": "локальний, у Wikidata немає"}}]

Теги:

{tags_block}"""


def _parse_response(text):
    """Витягти JSON-масив з відповіді Claude (навіть якщо обгорнутий у ```)."""
    text = text.strip()
    # Найперший '[' до останнього ']' — стійко до випадкового обрамлення.
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("у відповіді немає JSON-масиву")
    return json.loads(text[start:end + 1])


async def _map_batch(batch):
    """Один виклик Claude на батч. Повертає dict tag_id → рішення
    {qid, type, label, confidence, reason}, вже валідоване."""
    message = await async_client.messages.create(
        model=FOX_MODEL_SMART,
        max_tokens=2000,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": _build_prompt(batch)}],
    )
    _record_usage(FOX_MODEL_SMART, message.usage)
    text = "".join(b.text for b in message.content if b.type == "text")
    decisions = _parse_response(text)

    valid_qids = {c["id"] for item in batch for c in item["candidates"]}
    result = {}
    for d in decisions:
        try:
            tag_id = int(d.get("tag_id"))
        except (TypeError, ValueError):
            continue
        qid = d.get("qid")
        # Захист від галюцинації: приймаємо QID лише якщо він реально був
        # серед кандидатів цього прогону і має валідний формат.
        if qid and (not _QID_RE.match(str(qid)) or qid not in valid_qids):
            qid = None
        stype = d.get("type")
        if stype not in SCHEMA_TYPES:
            stype = "Thing" if qid else None
        try:
            conf = float(d.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        result[tag_id] = {
            "qid": qid,
            "type": stype,
            "label": (d.get("label") or "").strip() or None,
            "confidence": max(0.0, min(1.0, conf)),
            "reason": (d.get("reason") or "").strip(),
        }
    return result


# ---------- Складання прогону ----------

async def run_mapping(n, progress_cb=None):
    """Повний прогін: топ-N тегів → кандидати Wikidata → рішення Claude.
    Повертає list рядків результату (по одному на тег), збагачених рішенням.
    progress_cb(done, total) — опційний async-колбек прогресу."""
    tags = await asyncio.to_thread(_fetch_top_tags, n)
    total = len(tags)

    # Кандидати Wikidata по кожному тегу (мережа — у потоці).
    enriched = []
    for i, tag in enumerate(tags, 1):
        candidates = await asyncio.to_thread(_candidates_for, tag)
        enriched.append({"tag": tag, "candidates": candidates})
        if progress_cb and (i % 10 == 0 or i == total):
            await progress_cb("wiki", i, total)

    # Рішення Claude батчами.
    decisions = {}
    done = 0
    for start in range(0, total, BATCH):
        batch = enriched[start:start + BATCH]
        try:
            decisions.update(await _map_batch(batch))
        except Exception as e:
            print(f"tags_wiki: батч {start}-{start+len(batch)} не вдався — {e}")
        done += len(batch)
        if progress_cb:
            await progress_cb("ai", done, total)

    rows = []
    for item in enriched:
        t = item["tag"]
        d = decisions.get(t["id"], {})
        rows.append({
            **t,
            "candidates": item["candidates"],
            "qid": d.get("qid"),
            "type": d.get("type"),
            "chosen_label": d.get("label"),
            "confidence": d.get("confidence", 0.0),
            "reason": d.get("reason", ""),
        })
    return rows


# ---------- Файли на вихід ----------

def _tags_csv(tags):
    """CSV топ-тегів (для /tags_export): id, назви, рубрика, ужиток."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["tag_id", "usage", "name_ua", "name_ru", "name_en",
                "google_category", "description"])
    for t in tags:
        w.writerow([t["id"], t["usage"], t["name"], t["name_ru"],
                    t["name_en"], t["google_category"], t["description"]])
    return buf.getvalue().encode("utf-8")


def _review_csv(rows):
    """CSV на ревʼю: рішення + кандидати, щоб редакція звірила очима."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["tag_id", "usage", "name_ua", "name_ru", "qid", "type",
                "chosen_label", "confidence", "wikidata_url", "reason",
                "candidates"])
    for r in rows:
        url = f"https://www.wikidata.org/wiki/{r['qid']}" if r["qid"] else ""
        cand = " | ".join(
            f"{c['id']}:{c['label']}" for c in r["candidates"]
        )
        w.writerow([
            r["id"], r["usage"], r["name"], r["name_ru"],
            r["qid"] or "", r["type"] or "", r["chosen_label"] or "",
            f"{r['confidence']:.2f}", url, r["reason"], cand,
        ])
    return buf.getvalue().encode("utf-8")


def _sanitize_comment(text):
    """Текст для SQL-коментаря: без переносів рядків і послідовності */."""
    return (text or "").replace("\n", " ").replace("*/", "* /").strip()


def _sql_script(rows):
    """SQL: ALTER (додати колонки) + UPDATE по кожному впевненому тегу.
    Невпевнені (confidence < CONF_APPLY) — закоментовані для ручного ревʼю.
    В UPDATE потрапляють ЛИШЕ валідовані qid (^Q\\d+$) і type з білого списку
    + числовий id — вільний текст іде тільки в коментарі. Інʼєкція неможлива."""
    lines = [
        "-- Прив'язка тегів до Wikidata (schema.org about). Згенеровано ботом.",
        f"-- Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-- Застосувати в PHPMyAdmin на БД сайту (bot має тільки SELECT).",
        "--",
        "-- 1) Додати колонки (якщо ще немає). Якщо колонки вже є — цей рядок",
        "--    дасть помилку 'Duplicate column' — тоді просто пропусти його.",
        "ALTER TABLE `tag`",
        "  ADD COLUMN `wikidata_qid` VARCHAR(20) NULL,",
        "  ADD COLUMN `wikidata_type` VARCHAR(32) NULL;",
        "",
        "-- 2) Наповнення. Закоментовані рядки (-- LOW) — низька впевненість,",
        "--    звір вручну по CSV перед розкоментуванням.",
        "",
    ]
    applied = low = skipped = 0
    for r in rows:
        if not r["qid"]:
            skipped += 1
            continue
        note = _sanitize_comment(
            f"{r['name']} → {r['chosen_label'] or ''} "
            f"[{r['type']}] conf {r['confidence']:.2f}"
        )
        stmt = (
            f"UPDATE `tag` SET `wikidata_qid`='{r['qid']}', "
            f"`wikidata_type`='{r['type']}' WHERE `id`={int(r['id'])};"
        )
        if r["confidence"] >= CONF_APPLY:
            lines.append(f"{stmt}  -- {note}")
            applied += 1
        else:
            lines.append(f"-- LOW {stmt}  -- {note}")
            low += 1
    lines.append("")
    lines.append(
        f"-- Підсумок: {applied} застосовних, {low} низька впевненість "
        f"(закоментовані), {skipped} без відповідника у Wikidata."
    )
    return "\n".join(lines).encode("utf-8")


# ---------- Команди ----------

async def tags_export_handler(update, context):
    """/tags_export [N] — топ-N тегів за ужитком у CSV (за замовч. 300)."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not db.is_configured():
        await update.message.reply_text("🦊 БД сайту не налаштована (DB_* env).")
        return
    n = 300
    if context.args:
        try:
            n = max(1, int(context.args[0]))
        except ValueError:
            pass
    msg = await update.message.reply_text(f"🦊 Збираю топ-{n} тегів за ужитком…")
    try:
        tags = await asyncio.to_thread(_fetch_top_tags, n)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось прочитати теги: {e}")
        return
    data = await asyncio.to_thread(_tags_csv, tags)
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=f"tags_top_{len(tags)}.csv",
        caption=f"🦊 {len(tags)} тегів за спаданням ужитку.",
    )
    await msg.delete()


async def tags_wiki_handler(update, context):
    """/tags_wiki [N] — зіставити топ-N тегів із Wikidata (за замовч. 100),
    віддати CSV на ревʼю + .sql (ALTER + UPDATE). Працює у фоні."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not db.is_configured():
        await update.message.reply_text("🦊 БД сайту не налаштована (DB_* env).")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        await update.message.reply_text("🦊 Немає ANTHROPIC_API_KEY — нема чим зіставляти.")
        return
    if _running["flag"]:
        await update.message.reply_text("🦊 Прогін уже йде — дочекайся його.")
        return

    n = 100
    if context.args:
        try:
            n = max(1, int(context.args[0]))
        except ValueError:
            pass

    msg = await update.message.reply_text(
        f"🦊 Зіставляю топ-{n} тегів із Wikidata. Це кілька хвилин…"
    )
    state = {"last_edit": 0.0}

    async def progress(phase, done, total):
        now = asyncio.get_event_loop().time()
        if now - state["last_edit"] < 8 and done < total:
            return
        state["last_edit"] = now
        label = "шукаю у Wikidata" if phase == "wiki" else "Claude добирає сутності"
        try:
            await msg.edit_text(f"🦊 {label}: {done}/{total}…")
        except Exception:
            pass

    async def task():
        _running["flag"] = True
        try:
            rows = await run_mapping(n, progress_cb=progress)
            matched = sum(1 for r in rows if r["qid"])
            strong = sum(1 for r in rows if r["qid"] and r["confidence"] >= CONF_APPLY)
            review = await asyncio.to_thread(_review_csv, rows)
            sql = await asyncio.to_thread(_sql_script, rows)
            stamp = datetime.now().strftime("%Y%m%d")
            await update.message.reply_document(
                document=io.BytesIO(review),
                filename=f"tags_wikidata_review_{stamp}.csv",
                caption=(
                    f"🦊 Зіставлено {matched}/{len(rows)} тегів "
                    f"({strong} впевнено ≥{CONF_APPLY:.0%}).\n"
                    "Це ревʼю: звір кандидатів і впевненість очима."
                ),
            )
            await update.message.reply_document(
                document=io.BytesIO(sql),
                filename=f"tags_wikidata_{stamp}.sql",
                caption=(
                    "🦊 Готовий SQL. Застосуй у PHPMyAdmin:\n"
                    "1) ALTER додасть колонки wikidata_qid/type;\n"
                    "2) UPDATE наповнить упевнені теги;\n"
                    f"3) рядки «-- LOW» (впевненість <{CONF_APPLY:.0%}) — "
                    "звір вручну й розкоментуй за потреби."
                ),
            )
            await msg.delete()
        except Exception as e:
            try:
                await msg.edit_text(f"❌ Прогін зіставлення обірвався: {e}")
            except Exception:
                pass
        finally:
            _running["flag"] = False

    asyncio.create_task(task())
