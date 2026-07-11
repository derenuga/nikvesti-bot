"""
Версії бюджетного плану Миколаєва + рішення про зміни (задание №2, схема budget).

Горсовет вносить зміни в бюджет рішеннями сесії «викласти додаток у новій
редакції» — повною заміною, без явних дельт. До кожного рішення публікується
пакет «Порівняльні таблиці»: xlsx, де на одному листі бок о бок стоять
«Чинна редакція» (лівий блок колонок) і «Запропонована редакція з урахуванням
змін» (правий блок), порядково вирівняні. Дельти рахуємо як права мінус ліва.
Саме ці xlsx — джерело завантаження; PDF-додатки рішень не парсимо.

Зміни плану бувають і БЕЗ рішень сесії (розпорядження мера при зміні
трансфертів, рішення виконкому по постанові КМУ №18) — тому сума дельт рішень
НЕ зобов'язана пояснювати всю динаміку плану: щомісячні снапшоти (задание №1,
у репо ще НЕ реалізоване) залишаються первинним фактом, рішення дають атрибуцію.
Звірка ревізій зі снапшотами додасться, коли з'явиться задание №1 — цей модуль
існуючих таблиць не чіпає, тільки додає свої.

Структура порівняльної таблиці (перевірено на реальному файлі
«s-fi-005 Додаток 3 Порівняльна таблиця .xlsx», департамент фінансів ММР):
- рядок-заголовок з підписом «Чинна редакція» / «Запропонована редакція …»;
- рядок нумерації колонок: 1..16 | 1..16 (видатки, Додаток 3) — межу блоків
  шукаємо ПО ПОВТОРНОМУ СТАРТУ нумерації з «1», не по фіксованому номеру колонки;
  доходи (Додаток 1) — 1..6 | 1..6;
- коди лежать числами, ведучі нулі втрачені (210160.0 замість '0210160') —
  zero-pad: КПКВК до 7, ТПКВК до 4, КФК до 4, код доходу до 8, код бюджету
  1.4549E9 → '1454900000';
- рядки-підсумки розпорядників (КПКВК виду XX00000/XX10000, без ТПКВК/КФК) —
  is_unit_total = true, в аналітиці по програмах виключаються;
- підсумковий рядок «УСЬОГО» (коди = 'Х') — не зберігається, йде на контроль сум;
- нова програма = порожня ліва частина рядка (в валідацію не пишеться);
- xlsx лише з ОДНИМ блоком (без порівняння) — додаток вихідного рішення,
  вантажиться як kind='original'.

Ліва частина порівняльної таблиці НЕ зберігається як дані — тільки валідація
проти останньої збереженої ревізії року; розбіжності пишуться в
budget.revision_validation_issue і НЕ зупиняють завантаження (розбіжності —
реальність: у s-fi-001 «чинна редакція» містить друкарську помилку депфіну по
рядках 0200000/0210000 — різне розбиття споживання/розвиток при рівних
підсумках). Якщо ревізій року ще немає — ліва частина першої порівняльної
таблиці стає ревізією kind='original' з notes='reconstructed from comparison
table' (первинне рішення часто доступне лише в PDF).

Команди:
    /budget_load <рік> <номер> <дата> [base=50/26]
        — надіслати БОТУ xlsx документом з цією командою в підписі (caption),
          або відповісти командою на повідомлення з xlsx. Приклад:
          /budget_load 2026 50/123 29.01.2026 base=50/26
    /budget_status               — ревізії по роках, суми, розбіжності валідації
    /budget_headline <рік> <номер> ключ=значення …
        — показники з текстової частини рішення (вручну/напівавтоматом),
          ключі: revenue_total, revenue_general, revenue_special,
          expenditure_total, expenditure_general, expenditure_special,
          reserve_fund, debt_limit, guaranteed_debt_limit, staff_count

Тихо не працює без BOT_DATABASE_URL — як archive_mirror/analytics_store.
"""

import asyncio
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from handlers import bot_db

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# ---------- Схема (тільки нові таблиці, існуючих не чіпаємо) ----------

_BUDGET_SCHEMA_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS budget",
    # Реєстр редакцій плану: вихідне рішення + кожна зміна
    """
    CREATE TABLE IF NOT EXISTS budget.plan_revision (
        id              serial PRIMARY KEY,
        fiscal_year     int NOT NULL,
        decision_number text NOT NULL,
        decision_date   date,
        kind            text NOT NULL CHECK (kind IN ('original','amendment')),
        effective_order int NOT NULL,
        source_file     text,
        notes           text,
        UNIQUE (fiscal_year, effective_order)
    )
    """,
    # Рядки видаткової частини (структура Додатка 3)
    """
    CREATE TABLE IF NOT EXISTS budget.plan_expenditure_line (
        id                  bigserial PRIMARY KEY,
        revision_id         int NOT NULL REFERENCES budget.plan_revision,
        kpkvk               text NOT NULL,
        tpkvk               text,
        kfk                 text,
        line_name           text NOT NULL,
        is_unit_total       boolean DEFAULT false,
        general_total       numeric(16,2),
        general_consumption numeric(16,2),
        general_salary      numeric(16,2),
        general_utilities   numeric(16,2),
        general_development numeric(16,2),
        special_total       numeric(16,2),
        special_dev_budget  numeric(16,2),
        special_consumption numeric(16,2),
        special_salary      numeric(16,2),
        special_utilities   numeric(16,2),
        special_development numeric(16,2),
        total               numeric(16,2),
        UNIQUE (revision_id, kpkvk)
    )
    """,
    # Рядки дохідної частини (структура Додатка 1)
    """
    CREATE TABLE IF NOT EXISTS budget.plan_revenue_line (
        id                 bigserial PRIMARY KEY,
        revision_id        int NOT NULL REFERENCES budget.plan_revision,
        code               text NOT NULL,
        line_name          text NOT NULL,
        total              numeric(16,2),
        general_fund       numeric(16,2),
        special_fund       numeric(16,2),
        special_dev_budget numeric(16,2),
        UNIQUE (revision_id, code)
    )
    """,
    # Заголовні показники з текстової частини рішення (вручну/напівавтоматом)
    """
    CREATE TABLE IF NOT EXISTS budget.plan_headline (
        revision_id           int PRIMARY KEY REFERENCES budget.plan_revision,
        revenue_total         numeric(16,2),
        revenue_general       numeric(16,2),
        revenue_special       numeric(16,2),
        expenditure_total     numeric(16,2),
        expenditure_general   numeric(16,2),
        expenditure_special   numeric(16,2),
        reserve_fund          numeric(16,2),
        debt_limit            numeric(16,2),
        guaranteed_debt_limit numeric(16,2),
        staff_count           int
    )
    """,
    # Розбіжності «чинної редакції» документа зі збереженою ревізією.
    # Розбіжності — реальність (друкарські помилки депфіну), НЕ зупиняють load.
    """
    CREATE TABLE IF NOT EXISTS budget.revision_validation_issue (
        id             bigserial PRIMARY KEY,
        revision_id    int NOT NULL REFERENCES budget.plan_revision,
        table_kind     text NOT NULL,
        code           text NOT NULL,
        field          text NOT NULL,
        stored_value   numeric(16,2),
        document_value numeric(16,2),
        created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Дельти між сусідніми ревізіями (видатки). Підсумки розпорядників
    # виключено, щоб не задвоювати програми.
    """
    CREATE OR REPLACE VIEW budget.v_plan_amendments AS
    SELECT n.revision_id, r.decision_number, r.decision_date,
           n.kpkvk, n.line_name,
           n.total - COALESCE(p.total, 0)                 AS delta_total,
           n.general_total - COALESCE(p.general_total, 0) AS delta_general,
           n.special_total - COALESCE(p.special_total, 0) AS delta_special,
           (p.kpkvk IS NULL) AS is_new_program
    FROM budget.plan_expenditure_line n
    JOIN budget.plan_revision r ON r.id = n.revision_id AND r.kind = 'amendment'
    LEFT JOIN budget.plan_expenditure_line p
      ON p.kpkvk = n.kpkvk
     AND p.revision_id = (SELECT id FROM budget.plan_revision pr
                          WHERE pr.fiscal_year = r.fiscal_year
                            AND pr.effective_order = r.effective_order - 1)
    WHERE NOT n.is_unit_total
      AND (n.total IS DISTINCT FROM p.total OR p.kpkvk IS NULL)
    """,
    # Дельти дохідної частини — той самий принцип
    """
    CREATE OR REPLACE VIEW budget.v_plan_revenue_amendments AS
    SELECT n.revision_id, r.decision_number, r.decision_date,
           n.code, n.line_name,
           n.total - COALESCE(p.total, 0)                       AS delta_total,
           n.general_fund - COALESCE(p.general_fund, 0)         AS delta_general,
           n.special_fund - COALESCE(p.special_fund, 0)         AS delta_special,
           (p.code IS NULL) AS is_new_line
    FROM budget.plan_revenue_line n
    JOIN budget.plan_revision r ON r.id = n.revision_id AND r.kind = 'amendment'
    LEFT JOIN budget.plan_revenue_line p
      ON p.code = n.code
     AND p.revision_id = (SELECT id FROM budget.plan_revision pr
                          WHERE pr.fiscal_year = r.fiscal_year
                            AND pr.effective_order = r.effective_order - 1)
    WHERE n.total IS DISTINCT FROM p.total
       OR p.code IS NULL
    """,
    # Зниклі програми: рядок був у попередній ревізії, у новій його немає
    """
    CREATE OR REPLACE VIEW budget.v_plan_disappeared AS
    SELECT n.id AS revision_id, n.decision_number, n.decision_date,
           p.kpkvk, p.line_name, p.total AS last_total
    FROM budget.plan_revision n
    JOIN budget.plan_revision prev
      ON prev.fiscal_year = n.fiscal_year
     AND prev.effective_order = n.effective_order - 1
    JOIN budget.plan_expenditure_line p ON p.revision_id = prev.id
    WHERE n.kind = 'amendment'
      AND NOT p.is_unit_total
      AND EXISTS (SELECT 1 FROM budget.plan_expenditure_line x
                  WHERE x.revision_id = n.id)
      AND NOT EXISTS (SELECT 1 FROM budget.plan_expenditure_line x
                      WHERE x.revision_id = n.id AND x.kpkvk = p.kpkvk)
    """,
]

_ensure_done = False


def ensure_budget_schema():
    """Ідемпотентно створює схему budget (лениво, раз на процес)."""
    global _ensure_done
    if _ensure_done:
        return
    for sql in _BUDGET_SCHEMA_STATEMENTS:
        bot_db.execute(sql)
    _ensure_done = True


def is_ready():
    return bot_db.is_configured()


# ---------- Парсер порівняльної таблиці ----------

# Скільки знаків у кодах: зеро-пад втрачених ведучих нулів
KPKVK_LEN = 7
TPKVK_LEN = 4
KFK_LEN = 4
REVENUE_CODE_LEN = 8

# Колонки блоку видатків (Додаток 3), позиції після рядка нумерації 1..16
_EXP_FIELDS = [
    "kpkvk", "tpkvk", "kfk", "line_name",
    "general_total", "general_consumption", "general_salary",
    "general_utilities", "general_development",
    "special_total", "special_dev_budget", "special_consumption",
    "special_salary", "special_utilities", "special_development",
    "total",
]
# Колонки блоку доходів (Додаток 1), нумерація 1..6
_REV_FIELDS = ["code", "line_name", "total", "general_fund", "special_fund", "special_dev_budget"]

_MONEY_FIELDS_EXP = _EXP_FIELDS[4:]
_MONEY_FIELDS_REV = _REV_FIELDS[2:]


def _num_code(value, width):
    """210160.0 → '0210160'; 1.4549E9 → '1454900000'; 'Х'/порожнє → None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(round(value))).zfill(width)
    s = str(value).strip()
    if not s or s.upper() in ("Х", "X"):
        return None
    if re.fullmatch(r"\d+([.,]0+)?([eE]\+?\d+)?", s):
        try:
            return str(int(float(s.replace(",", ".")))).zfill(width)
        except ValueError:
            return None
    return None


def _money(value):
    """Грошова клітинка → Decimal(2 знаки) або None (порожньо)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(round(value, 2)))
    s = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s or s.upper() in ("Х", "X", "-"):
        return None
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _text(value):
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def _find_numbering_row(rows):
    """Шукає рядок нумерації колонок (1,2,3…). Повертає (індекс рядка,
    [(start_col, width), …] для кожного блока). Межа блоків — повторний старт
    нумерації з 1, НЕ фіксований номер колонки."""
    for i, row in enumerate(rows):
        starts = []  # (col, width)
        expect = None  # (start, next_expected)
        for col, v in enumerate(row):
            n = None
            if isinstance(v, (int, float)) and float(v).is_integer():
                n = int(v)
            elif isinstance(v, str) and v.strip().isdigit():
                n = int(v.strip())
            if n == 1:
                starts.append([col, 1])
                expect = 2
            elif expect and n == expect and starts:
                starts[-1][1] += 1
                expect += 1
            elif n is not None:
                expect = None
        # валідний рядок нумерації: 1..N (N≥5) один або два рази
        if starts and all(w >= 5 for _, w in starts):
            return i, [(c, w) for c, w in starts]
    return None, []


def _detect_table_kind(rows, block_width):
    """'expenditure' (Додаток 3) чи 'revenue' (Додаток 1) — за заголовками."""
    head_text = " ".join(
        str(v) for row in rows[:15] for v in row if isinstance(v, str)
    ).lower()
    if "програмної класифікації видатків" in head_text or block_width >= 12:
        return "expenditure"
    if "доход" in head_text or block_width <= 8:
        return "revenue"
    raise ValueError(f"Не впізнав тип таблиці (ширина блоку {block_width})")


def _parse_block_row(row, start, width, kind):
    """Зріз рядка листа → dict полів рядка бюджету або None (порожньо/розділ)."""
    cells = list(row[start:start + width])
    cells += [None] * (width - len(cells))
    if kind == "expenditure":
        kpkvk = _num_code(cells[0], KPKVK_LEN)
        if not kpkvk:
            return None
        name = _text(cells[3])
        if not name:
            return None
        tpkvk = _num_code(cells[1], TPKVK_LEN)
        line = {
            "kpkvk": kpkvk,
            "tpkvk": tpkvk,
            "kfk": _num_code(cells[2], KFK_LEN),
            "line_name": name,
            # Підсумок розпорядника: КПКВК XX00000/XX10000 і немає ТПКВК
            "is_unit_total": tpkvk is None and kpkvk[-5:] in ("00000", "10000"),
        }
        for f, c in zip(_MONEY_FIELDS_EXP, cells[4:16]):
            line[f] = _money(c)
        return line
    else:
        code = _num_code(cells[0], REVENUE_CODE_LEN)
        name = _text(cells[1])
        if not code or not name:
            return None
        line = {"code": code, "line_name": name}
        for f, c in zip(_MONEY_FIELDS_REV, cells[2:6]):
            line[f] = _money(c)
        return line


def _grand_total_row(row, start, width, kind):
    """Рядок 'УСЬОГО' (коди 'Х') → dict сум для контролю, або None."""
    cells = list(row[start:start + width])
    cells += [None] * (width - len(cells))
    name_idx = 3 if kind == "expenditure" else 1
    name = _text(cells[name_idx])
    if not name or "усього" not in name.lower():
        return None
    money_cells = cells[4:16] if kind == "expenditure" else cells[2:6]
    fields = _MONEY_FIELDS_EXP if kind == "expenditure" else _MONEY_FIELDS_REV
    return {f: _money(c) for f, c in zip(fields, money_cells)}


def parse_comparison_xlsx(data, filename=""):
    """Парсить xlsx порівняльної таблиці (або одноблочний додаток вихідного
    рішення). Повертає dict:
        kind        — 'expenditure' | 'revenue'
        blocks      — 1 (одноблочний оригінал) або 2 (порівняльна)
        left/right  — списки рядків (для одноблочного — тільки right)
        left_total/right_total — контрольні суми з рядка 'УСЬОГО' (або None)
    """
    import openpyxl  # локальний імпорт: бот стартує і без openpyxl

    wb = openpyxl.load_workbook(BytesIO(data), data_only=True, read_only=True)
    last_err = None
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        try:
            num_idx, blocks = _find_numbering_row(rows[:40])
        except Exception as e:  # noqa: BLE001 — пробуємо наступний лист
            last_err = e
            continue
        if num_idx is None:
            last_err = ValueError(f"лист '{sheet}': не знайшов рядок нумерації колонок")
            continue
        if len(blocks) not in (1, 2):
            last_err = ValueError(f"лист '{sheet}': {len(blocks)} блоків нумерації")
            continue

        width = blocks[0][1]
        if len(blocks) == 2 and blocks[1][1] != width:
            raise ValueError(
                f"Блоки різної ширини: {blocks[0][1]} і {blocks[1][1]} — нова структура файлу?"
            )
        kind = _detect_table_kind(rows, width)
        expected = 16 if kind == "expenditure" else 6
        if width != expected:
            raise ValueError(
                f"Ширина блоку {width}, очікував {expected} для {kind} — структура змінилась, перевір файл"
            )

        left_rows, right_rows = [], []
        left_total = right_total = None
        for row in rows[num_idx + 1:]:
            gt = _grand_total_row(row, blocks[-1][0], width, kind)
            if gt is not None:
                right_total = gt
                if len(blocks) == 2:
                    left_total = _grand_total_row(row, blocks[0][0], width, kind)
                break
            right = _parse_block_row(row, blocks[-1][0], width, kind)
            left = _parse_block_row(row, blocks[0][0], width, kind) if len(blocks) == 2 else None
            if len(blocks) == 2:
                if left or right:
                    left_rows.append(left)
                    right_rows.append(right)
            elif right:
                right_rows.append(right)

        if not right_rows:
            last_err = ValueError(f"лист '{sheet}': не знайшов жодного рядка даних")
            continue
        wb.close()
        return {
            "kind": kind,
            "blocks": len(blocks),
            "left": left_rows,
            "right": right_rows,
            "left_total": left_total,
            "right_total": right_total,
            "filename": filename,
        }
    wb.close()
    raise ValueError(f"Не розібрав xlsx: {last_err}")


# ---------- Завантажувач ----------

_LINE_COLS = {
    "expenditure": ("budget.plan_expenditure_line",
                    ["kpkvk", "tpkvk", "kfk", "line_name", "is_unit_total"] + _MONEY_FIELDS_EXP),
    "revenue": ("budget.plan_revenue_line", ["code", "line_name"] + _MONEY_FIELDS_REV),
}


def _insert_lines(cur, revision_id, kind, lines):
    import psycopg2.extras
    table, cols = _LINE_COLS[kind]
    key = cols[0]
    # Ідемпотентна заміна: старі рядки цієї таблиці ревізії — геть, нові — на місце
    cur.execute(f"DELETE FROM {table} WHERE revision_id = %s", (revision_id,))
    seen, rows, dupes = set(), [], 0
    for ln in lines:
        if ln[key] in seen:  # дубль коду в документі — перший виграє
            dupes += 1
            continue
        seen.add(ln[key])
        rows.append(tuple([revision_id] + [ln.get(c) for c in cols]))
    psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO {table} (revision_id, {', '.join(cols)}) VALUES %s",
        rows,
        page_size=200,
    )
    return len(rows), dupes


def _validate_left(cur, new_revision_id, kind, prev_revision_id, left_lines):
    """Звіряє «чинну редакцію» документа зі збереженою попередньою ревізією.
    Кожна розбіжність → рядок у revision_validation_issue. Нові програми
    (порожня ліва частина) сюди не потрапляють. Повертає к-сть розбіжностей."""
    table, cols = _LINE_COLS[kind]
    key = cols[0]
    money_fields = _MONEY_FIELDS_EXP if kind == "expenditure" else _MONEY_FIELDS_REV
    cur.execute(f"SELECT * FROM {table} WHERE revision_id = %s", (prev_revision_id,))
    colnames = [d[0] for d in cur.description]
    stored = {}
    for row in cur.fetchall():
        rec = dict(zip(colnames, row))
        stored[rec[key]] = rec

    issues = []
    doc_codes = set()
    for ln in left_lines:
        if ln is None:
            continue
        doc_codes.add(ln[key])
        st = stored.get(ln[key])
        if st is None:
            # рядок є в документі, у збереженій ревізії немає
            issues.append((ln[key], "presence", None, ln.get("total")))
            continue
        for f in money_fields:
            sv, dv = st.get(f), ln.get(f)
            if (sv or Decimal(0)) != (dv or Decimal(0)):
                issues.append((ln[key], f, sv, dv))
    for code, rec in stored.items():
        if code not in doc_codes:
            issues.append((code, "presence", rec.get("total"), None))

    for code, field, sv, dv in issues:
        cur.execute(
            "INSERT INTO budget.revision_validation_issue "
            "(revision_id, table_kind, code, field, stored_value, document_value) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (new_revision_id, kind, code, field, sv, dv),
        )
    return len(issues)


def load_comparison(data, fiscal_year, decision_number, decision_date=None,
                    base_decision=None, filename=""):
    """Завантажує один xlsx (порівняльний або одноблочний оригінал) у схему
    budget. Синхронна (psycopg2) — в боті викликати через asyncio.to_thread.

    Повертає dict-звіт для повідомлення в чат."""
    ensure_budget_schema()
    parsed = parse_comparison_xlsx(data, filename=filename)
    kind = parsed["kind"]

    conn = bot_db._connect()
    try:
        with conn, conn.cursor() as cur:
            # Попередня (остання) ревізія року
            cur.execute(
                "SELECT id, effective_order, decision_number FROM budget.plan_revision "
                "WHERE fiscal_year = %s ORDER BY effective_order DESC LIMIT 1",
                (fiscal_year,),
            )
            prev = cur.fetchone()

            report = {
                "kind": kind, "blocks": parsed["blocks"],
                "rows": len(parsed["right"]),
                "original_created": False, "validated": 0, "issues": 0,
                "revision_reused": False, "dupes": 0,
            }

            if parsed["blocks"] == 1:
                # Одноблочний xlsx = додаток вихідного рішення → original
                if prev is None:
                    cur.execute(
                        "INSERT INTO budget.plan_revision "
                        "(fiscal_year, decision_number, decision_date, kind, effective_order, source_file) "
                        "VALUES (%s, %s, %s, 'original', 0, %s) RETURNING id",
                        (fiscal_year, decision_number, decision_date, filename),
                    )
                    revision_id = cur.fetchone()[0]
                    report["original_created"] = True
                else:
                    cur.execute(
                        "SELECT id FROM budget.plan_revision "
                        "WHERE fiscal_year = %s AND decision_number = %s",
                        (fiscal_year, decision_number),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise ValueError(
                            f"Одноблочний файл, але в {fiscal_year} вже є ревізії "
                            f"і рішення {decision_number} серед них немає — "
                            "оригінал вантажиться першим"
                        )
                    revision_id = row[0]
                    report["revision_reused"] = True
                inserted, dupes = _insert_lines(cur, revision_id, kind, parsed["right"])
                report.update(revision_id=revision_id, inserted=inserted, dupes=dupes,
                              effective_order=0)
                return report

            # Порівняльна таблиця (2 блоки)
            left_present = [ln for ln in parsed["left"] if ln]
            if prev is None:
                # Ревізій року ще немає: ліва частина стає original
                orig_number = base_decision or "original"
                cur.execute(
                    "INSERT INTO budget.plan_revision "
                    "(fiscal_year, decision_number, decision_date, kind, effective_order, "
                    " source_file, notes) "
                    "VALUES (%s, %s, NULL, 'original', 0, %s, "
                    "        'reconstructed from comparison table') RETURNING id",
                    (fiscal_year, orig_number, filename),
                )
                _insert_lines(cur, cur.fetchone()[0], kind, left_present)
                report["original_created"] = True
            # Ревізія цього рішення: повторне завантаження не створює дубля
            cur.execute(
                "SELECT id, effective_order FROM budget.plan_revision "
                "WHERE fiscal_year = %s AND decision_number = %s",
                (fiscal_year, decision_number),
            )
            row = cur.fetchone()
            if row:
                revision_id, order = row
                report["revision_reused"] = True
            else:
                cur.execute(
                    "SELECT COALESCE(MAX(effective_order), -1) + 1 "
                    "FROM budget.plan_revision WHERE fiscal_year = %s",
                    (fiscal_year,),
                )
                order = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO budget.plan_revision "
                    "(fiscal_year, decision_number, decision_date, kind, effective_order, source_file) "
                    "VALUES (%s, %s, %s, 'amendment', %s, %s) RETURNING id",
                    (fiscal_year, decision_number, decision_date, order, filename),
                )
                revision_id = cur.fetchone()[0]

            # Попередник САМЕ ЦІЄЇ ревізії (order-1), а не остання ревізія року:
            # при повторному завантаженні остання — це вона сама
            cur.execute(
                "SELECT id, notes FROM budget.plan_revision "
                "WHERE fiscal_year = %s AND effective_order = %s",
                (fiscal_year, order - 1),
            )
            pred = cur.fetchone()
            pred_id = pred[0] if pred else None

            # original міг бути реконструйований БЕЗ цієї таблиці (перший файл
            # рішення був іншого додатка) — тоді доллємо його з лівої частини
            if pred and not report["original_created"]:
                table, _ = _LINE_COLS[kind]
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE revision_id = %s", (pred_id,)
                )
                if cur.fetchone()[0] == 0 and "reconstructed" in (pred[1] or ""):
                    _insert_lines(cur, pred_id, kind, left_present)
                    report["original_created"] = True

            right_present = [ln for ln in parsed["right"] if ln]
            inserted, dupes = _insert_lines(cur, revision_id, kind, right_present)

            # Валідація лівої частини проти попередника (якщо є з чим порівнювати
            # і це не щойно реконструйований з цього ж файлу original)
            issues = 0
            if pred_id and not report["original_created"]:
                # повторне завантаження: чистимо старі issues цієї таблиці
                cur.execute(
                    "DELETE FROM budget.revision_validation_issue "
                    "WHERE revision_id = %s AND table_kind = %s",
                    (revision_id, kind),
                )
                issues = _validate_left(cur, revision_id, kind, pred_id, left_present)
                report["validated"] = len(left_present)
            report.update(revision_id=revision_id, inserted=inserted, dupes=dupes,
                          issues=issues, effective_order=order)

            # Контроль сум: рядок «УСЬОГО» документа vs сума завантажених рядків
            gt = parsed.get("right_total")
            if gt and gt.get("total") is not None:
                table, _ = _LINE_COLS[kind]
                flt = "AND NOT is_unit_total" if kind == "expenditure" else ""
                cur.execute(
                    f"SELECT COALESCE(SUM(total), 0) FROM {table} "
                    f"WHERE revision_id = %s {flt}",
                    (revision_id,),
                )
                own_sum = cur.fetchone()[0]
                report["doc_total"] = gt["total"]
                report["sum_total"] = own_sum
                report["total_match"] = (own_sum == gt["total"])
            return report
    finally:
        conn.close()


# ---------- Аналітика для звіту в чат ----------

def amendments_summary(revision_id, kind, limit=8):
    """Найбільші дельти ревізії для повідомлення в чат."""
    view = "budget.v_plan_amendments" if kind == "expenditure" else "budget.v_plan_revenue_amendments"
    code_col = "kpkvk" if kind == "expenditure" else "code"
    new_col = "is_new_program" if kind == "expenditure" else "is_new_line"
    return bot_db.query(
        f"SELECT {code_col} AS code, line_name, delta_total, {new_col} AS is_new "
        f"FROM {view} WHERE revision_id = %s AND delta_total <> 0 "
        f"ORDER BY ABS(delta_total) DESC LIMIT %s",
        (revision_id, limit),
    )


# ---------- Telegram-хендлери ----------

def _fmt_money(v):
    if v is None:
        return "—"
    return f"{v:,.0f}".replace(",", " ")


def _parse_date(s):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


async def budget_load_handler(update, context):
    """/budget_load <рік> <номер> <дата> [base=50/26] — у підписі до xlsx
    або як reply на повідомлення з xlsx."""
    msg = update.effective_message
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await msg.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await msg.reply_text("Нора не налаштована (BOT_DATABASE_URL) — вантажити нікуди.")
        return

    doc = msg.document or (msg.reply_to_message.document if msg.reply_to_message else None)
    if not doc or not (doc.file_name or "").lower().endswith(".xlsx"):
        await msg.reply_text(
            "Потрібен xlsx: надішли файл з підписом\n"
            "/budget_load <рік> <номер рішення> <дата> [base=50/26]\n"
            "або дай цю команду відповіддю на повідомлення з файлом."
        )
        return

    text = (msg.caption or msg.text or "").strip()
    args = text.split()[1:]
    base_decision = None
    plain = []
    for a in args:
        if a.startswith("base="):
            base_decision = a[5:]
        else:
            plain.append(a)
    if len(plain) < 2:
        await msg.reply_text(
            "Мало аргументів. Формат: /budget_load 2026 50/123 29.01.2026 [base=50/26]"
        )
        return
    try:
        fiscal_year = int(plain[0])
    except ValueError:
        await msg.reply_text(f"Рік не число: {plain[0]}")
        return
    decision_number = plain[1]
    decision_date = _parse_date(plain[2]) if len(plain) > 2 else None

    progress = await msg.reply_text(f"🦊 Розбираю {doc.file_name}…")
    try:
        f = await context.bot.get_file(doc.file_id)
        data = bytes(await f.download_as_bytearray())
        report = await asyncio.to_thread(
            load_comparison, data, fiscal_year, decision_number, decision_date,
            base_decision, doc.file_name,
        )
    except Exception as e:  # noqa: BLE001 — повідомляємо в чат, не мовчимо
        await progress.edit_text(f"❌ Не завантажилось: {e}")
        return

    kind_ua = "видатки (Додаток 3)" if report["kind"] == "expenditure" else "доходи (Додаток 1)"
    lines = [
        f"✅ {kind_ua}: рішення {decision_number}/{fiscal_year}, "
        f"ревізія #{report['effective_order']}, {report['inserted']} рядків",
    ]
    if report["original_created"]:
        lines.append("📌 Ревізій року не було — ліву частину збережено як original "
                     "(reconstructed from comparison table)")
    if report["revision_reused"]:
        lines.append("♻️ Ревізія вже існувала — рядки цієї таблиці перезаписано без дубля")
    if report.get("validated"):
        if report["issues"]:
            lines.append(f"⚠️ Валідація «чинної редакції»: {report['issues']} розбіжностей "
                         f"зі збереженою ревізією → budget.revision_validation_issue")
        else:
            lines.append(f"✔️ «Чинна редакція» збігається зі збереженою ({report['validated']} рядків)")
    if report.get("dupes"):
        lines.append(f"⚠️ Дублі кодів у документі: {report['dupes']} (перший виграв)")
    if "total_match" in report:
        if report["total_match"]:
            lines.append(f"✔️ Контроль сум: {_fmt_money(report['doc_total'])} грн, збігається")
        else:
            lines.append(f"⚠️ Контроль сум: документ {_fmt_money(report['doc_total'])}, "
                         f"сума рядків {_fmt_money(report['sum_total'])}")

    deltas = await bot_db.aquery(
        "SELECT 1 FROM budget.plan_revision WHERE id = %s AND kind = 'amendment'",
        (report["revision_id"],),
    )
    if deltas:
        top = await asyncio.to_thread(amendments_summary, report["revision_id"], report["kind"])
        if top:
            lines.append("\nНайбільші зміни:")
            for t in top:
                mark = "🆕 " if t["is_new"] else ""
                sign = "+" if t["delta_total"] > 0 else ""
                lines.append(f"• {mark}{t['code']} {t['line_name'][:60]}: "
                             f"{sign}{_fmt_money(t['delta_total'])} грн")
    await progress.edit_text("\n".join(lines))


async def budget_status_handler(update, context):
    """/budget_status — ревізії по роках, суми, розбіжності валідації."""
    msg = update.effective_message
    if not is_ready():
        await msg.reply_text("Нора не налаштована (BOT_DATABASE_URL).")
        return
    try:
        await asyncio.to_thread(ensure_budget_schema)
        revs = await bot_db.aquery(
            """
            SELECT r.id, r.fiscal_year, r.decision_number, r.decision_date, r.kind,
                   r.effective_order, r.notes,
                   (SELECT COUNT(*) FROM budget.plan_expenditure_line e
                     WHERE e.revision_id = r.id) AS exp_lines,
                   (SELECT COALESCE(SUM(e.total),0) FROM budget.plan_expenditure_line e
                     WHERE e.revision_id = r.id AND NOT e.is_unit_total) AS exp_total,
                   (SELECT COUNT(*) FROM budget.plan_revenue_line v
                     WHERE v.revision_id = r.id) AS rev_lines,
                   (SELECT COALESCE(SUM(v.total),0) FROM budget.plan_revenue_line v
                     WHERE v.revision_id = r.id) AS rev_total,
                   (SELECT COUNT(*) FROM budget.revision_validation_issue i
                     WHERE i.revision_id = r.id) AS issues
            FROM budget.plan_revision r
            ORDER BY r.fiscal_year, r.effective_order
            """
        )
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(f"❌ {e}")
        return
    if not revs:
        await msg.reply_text(
            "Схема budget порожня. Надішли xlsx порівняльної таблиці з командою "
            "/budget_load <рік> <номер> <дата>."
        )
        return
    lines, year = ["🦊 Бюджет у норі:"], None
    for r in revs:
        if r["fiscal_year"] != year:
            year = r["fiscal_year"]
            lines.append(f"\n📅 {year}:")
        note = " (reconstructed)" if (r["notes"] or "").startswith("reconstructed") else ""
        d = f" від {r['decision_date'].strftime('%d.%m.%Y')}" if r["decision_date"] else ""
        parts = []
        if r["exp_lines"]:
            parts.append(f"видатки {r['exp_lines']} рядків, {_fmt_money(r['exp_total'])} грн")
        if r["rev_lines"]:
            parts.append(f"доходи {r['rev_lines']} рядків, {_fmt_money(r['rev_total'])} грн")
        if r["issues"]:
            parts.append(f"⚠️ {r['issues']} розбіжностей")
        lines.append(
            f"#{r['effective_order']} {r['kind']} {r['decision_number']}{d}{note} — "
            + ("; ".join(parts) if parts else "порожня")
        )
    lines.append(
        "\nℹ️ Звірка з місячними снапшотами плану з'явиться разом із заданням №1 "
        "(таблиць снапшотів у норі ще немає)."
    )
    await msg.reply_text("\n".join(lines))


_HEADLINE_FIELDS = {
    "revenue_total", "revenue_general", "revenue_special",
    "expenditure_total", "expenditure_general", "expenditure_special",
    "reserve_fund", "debt_limit", "guaranteed_debt_limit", "staff_count",
}


async def budget_headline_handler(update, context):
    """/budget_headline <рік> <номер> ключ=значення … — показники з текстової
    частини рішення (порівняльна таблиця текст.docx), вводяться вручну."""
    msg = update.effective_message
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await msg.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await msg.reply_text("Нора не налаштована (BOT_DATABASE_URL).")
        return
    args = context.args or []
    if len(args) < 3:
        await msg.reply_text(
            "Формат: /budget_headline 2026 50/123 revenue_total=6370506270 "
            "expenditure_total=6099438519 …\nКлючі: " + ", ".join(sorted(_HEADLINE_FIELDS))
        )
        return
    try:
        fiscal_year = int(args[0])
    except ValueError:
        await msg.reply_text(f"Рік не число: {args[0]}")
        return
    decision_number = args[1]
    values = {}
    for pair in args[2:]:
        if "=" not in pair:
            await msg.reply_text(f"Не ключ=значення: {pair}")
            return
        k, v = pair.split("=", 1)
        if k not in _HEADLINE_FIELDS:
            await msg.reply_text(f"Невідомий ключ: {k}")
            return
        try:
            values[k] = int(v) if k == "staff_count" else Decimal(v.replace(" ", ""))
        except (ValueError, InvalidOperation):
            await msg.reply_text(f"Не число: {pair}")
            return
    try:
        await asyncio.to_thread(ensure_budget_schema)
        rev = await bot_db.aquery(
            "SELECT id FROM budget.plan_revision WHERE fiscal_year = %s AND decision_number = %s",
            (fiscal_year, decision_number),
        )
        if not rev:
            await msg.reply_text(
                f"Ревізії {decision_number}/{fiscal_year} немає — спочатку /budget_load."
            )
            return
        revision_id = rev[0]["id"]
        cols = list(values.keys())
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
        await asyncio.to_thread(
            bot_db.execute,
            f"INSERT INTO budget.plan_headline (revision_id, {', '.join(cols)}) "
            f"VALUES (%s, {', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (revision_id) DO UPDATE SET {sets}",
            tuple([revision_id] + [values[c] for c in cols]),
        )
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(f"❌ {e}")
        return
    await msg.reply_text(
        f"✅ Заголовні показники {decision_number}/{fiscal_year}: "
        + ", ".join(f"{k}={_fmt_money(v)}" for k, v in values.items())
    )
