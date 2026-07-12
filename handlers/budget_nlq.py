"""
NLQ-tool query_budget: природномовні питання про бюджет Миколаєва до нори.

Дає Лису (handlers/query_router.py) доступ до схеми budget:
- ревізії плану з рішень сесії (budget_revisions): «що змінило останнє
  рішення?», «кому +139 659 000 грн?», «які нові програми?»;
- історія рядка по ревізіях: «як мінявся план по укриттях?»;
- місячні снапшоти виконання (budget_snapshots): «хто не виконує бюджет?»,
  «як освоює гроші УКБ?».

«Кому гроші» = розпорядник: перші 2 цифри КПКВК — код розпорядника (КВК),
назви беремо зі снапшотів (там вони людські), фолбек — рядки-підсумки
XX00000 у ревізії.

Всі суми повертаються числами (float) — Decimal не серіалізується в JSON
у циклі tool use. Синхронний код, викликається через asyncio.to_thread
у query_router (як решта tools).
"""

from decimal import Decimal

from handlers import bot_db

_LIMIT_MAX = 30


def _f(v):
    """Decimal/None → float/None для JSON."""
    if v is None:
        return None
    return float(v)


def _latest_year():
    rows = bot_db.query("SELECT MAX(fiscal_year) AS y FROM budget.plan_revision")
    if rows and rows[0]["y"]:
        return rows[0]["y"]
    rows = bot_db.query("SELECT MAX(fiscal_year) AS y FROM budget.snapshot")
    return rows[0]["y"] if rows and rows[0]["y"] else None


def _unit_names(fiscal_year):
    """КВК → назва розпорядника: спершу зі снапшотів (людські назви),
    фолбек — підсумкові рядки XX00000 останньої ревізії."""
    names = {}
    # снапшоти дають людські назви розпорядників; таблиці може ще не бути,
    # якщо жоден снапшот не завантажено — тоді лише фолбек з ревізій
    try:
        rows = bot_db.query(
            """SELECT DISTINCT ON (l.kvk) l.kvk, l.unit_name
               FROM budget.snapshot_expenditure_line l
               JOIN budget.snapshot s ON s.id = l.snapshot_id
               WHERE s.fiscal_year = %s AND l.code_type = 'unit'
               ORDER BY l.kvk, s.snapshot_date DESC""",
            (fiscal_year,),
        )
        for r in rows:
            names[r["kvk"]] = r["unit_name"]
    except Exception:  # noqa: BLE001 — таблиці снапшотів ще немає
        pass
    rows = bot_db.query(
        """SELECT DISTINCT ON (substr(e.kpkvk,1,2)) substr(e.kpkvk,1,2) AS kvk, e.line_name
           FROM budget.plan_expenditure_line e
           JOIN budget.plan_revision r ON r.id = e.revision_id
           WHERE r.fiscal_year = %s AND e.is_unit_total AND e.kpkvk LIKE '%%00000'
           ORDER BY substr(e.kpkvk,1,2), r.effective_order DESC""",
        (fiscal_year,),
    )
    for r in rows:
        names.setdefault(r["kvk"], r["line_name"])
    return names


def _norm_code(code, width):
    code = str(code).strip()
    return code.zfill(width) if code.isdigit() else code


def query_budget(query_type, fiscal_year=None, kind="expenditure", decision=None,
                 code=None, name_contains=None, min_amount=None, month=None, limit=10):
    """Єдиний вхід NLQ-tool. Повертає dict під json.dumps."""
    if not bot_db.is_configured():
        return {"error": "Нора (BOT_DATABASE_URL) не налаштована"}
    limit = min(int(limit or 10), _LIMIT_MAX)
    fiscal_year = fiscal_year or _latest_year()
    if not fiscal_year:
        return {"error": "У норі ще немає бюджетних даних — завантаж пакети рішень"}

    if query_type == "amendments":
        return _amendments(fiscal_year, kind, decision, code, name_contains, min_amount, month, limit)
    if query_type == "line_history":
        return _line_history(fiscal_year, kind, code, name_contains, limit)
    if query_type == "revisions":
        return _revisions(fiscal_year)
    if query_type == "execution":
        return _execution(fiscal_year, name_contains, limit)
    if query_type == "execution_trend":
        return _execution_trend(fiscal_year)
    if query_type == "budget_growth":
        return _budget_growth(fiscal_year)
    return {"error": f"Невідомий query_type: {query_type}"}


_MONTH_UA = ["", "січ", "лют", "бер", "кві", "тра", "чер",
             "лип", "сер", "вер", "жов", "лис", "гру"]


def _execution_trend(fiscal_year):
    """Динаміка виконання видатків по місяцях (для графіка): % касових до
    плану звітного періоду з місячних знімків. Готове під render_chart."""
    try:
        rows = bot_db.query(
            """SELECT s.snapshot_date, l.pct
               FROM budget.snapshot s
               JOIN budget.snapshot_expenditure_line l
                 ON l.snapshot_id = s.id AND l.code_type = 'total'
               WHERE s.kind = 'expenditure' AND s.fiscal_year = %s AND l.pct IS NOT NULL
               ORDER BY s.snapshot_date""",
            (fiscal_year,),
        )
    except Exception:  # noqa: BLE001 — місячних знімків ще немає
        rows = []
    points = [{"label": _MONTH_UA[r["snapshot_date"].month] if r["snapshot_date"].day <= 2
               else r["snapshot_date"].strftime("%d.%m"),
               "pct": float(r["pct"])} for r in rows]
    return {"fiscal_year": fiscal_year, "metric": "execution_pct",
            "labels": [p["label"] for p in points],
            "values": [p["pct"] for p in points],
            "note": "Відсоток касового виконання видатків до плану звітного періоду "
                    "по місяцях. Для графіка — chart_type='line'. Порожньо, якщо "
                    "місячних знімків ще немає (/budget_snapshot_check)."}


def _budget_growth(fiscal_year):
    """Розмір бюджету (план видатків/доходів) по ревізіях рішень — динаміка,
    як рішення сесії нарощували бюджет. Готове під render_chart."""
    rows = bot_db.query(
        """SELECT r.effective_order, r.decision_number, r.decision_date,
                  (SELECT COALESCE(SUM(e.total),0) FROM budget.plan_expenditure_line e
                    WHERE e.revision_id = r.id AND NOT e.is_unit_total) AS exp_total,
                  (SELECT COALESCE(SUM(v.total),0) FROM budget.plan_revenue_line v
                    WHERE v.revision_id = r.id AND v.code ~ '^\\d0{7}$') AS rev_total
           FROM budget.plan_revision r
           WHERE r.fiscal_year = %s
           ORDER BY r.effective_order""",
        (fiscal_year,),
    )
    labels, exp_vals, rev_vals = [], [], []
    for r in rows:
        labels.append(r["decision_number"])
        exp_vals.append(round(float(r["exp_total"]) / 1e9, 3) if r["exp_total"] else None)
        rev_vals.append(round(float(r["rev_total"]) / 1e9, 3) if r["rev_total"] else None)
    return {"fiscal_year": fiscal_year, "unit": "млрд грн", "labels": labels,
            "expenditure_bln": exp_vals, "revenue_bln": rev_vals,
            "note": "Розмір бюджету по ревізіях рішень, млрд грн. Для графіка — "
                    "chart_type='line', дві серії (видатки/доходи) або одна. "
                    "labels — номери рішень у порядку ухвалення."}


def _amendments(fiscal_year, kind, decision, code, name_contains, min_amount, month, limit):
    """Дельти рішень: хто отримав/втратив гроші, нові програми."""
    is_exp = kind != "revenue"
    view = "budget.v_plan_amendments" if is_exp else "budget.v_plan_revenue_amendments"
    code_col = "kpkvk" if is_exp else "code"
    new_col = "is_new_program" if is_exp else "is_new_line"
    where, params = ["r.fiscal_year = %s"], [fiscal_year]
    if decision:
        where.append("v.decision_number ILIKE %s")
        params.append(f"%{decision}%")
    if month:
        # «зміни в березні» → ревізії з датою ухвалення в цьому місяці.
        # Якщо дат немає (пакет-проєкт без /budget_date) — впаде в порожньо,
        # тому нижче попереджаємо про це в note.
        where.append("EXTRACT(MONTH FROM r.decision_date) = %s")
        params.append(int(month))
    if code:
        where.append(f"v.{code_col} = %s")
        params.append(_norm_code(code, 7 if is_exp else 8))
    if name_contains:
        where.append("v.line_name ILIKE %s")
        params.append(f"%{name_contains}%")
    if min_amount:
        where.append("ABS(v.delta_total) >= %s")
        params.append(min_amount)
    rows = bot_db.query(
        f"""SELECT v.decision_number, r.decision_date, v.{code_col} AS code, v.line_name,
                   v.delta_total, v.delta_general, v.delta_special, v.{new_col} AS is_new
            FROM {view} v
            JOIN budget.plan_revision r ON r.id = v.revision_id
            WHERE {' AND '.join(where)}
            ORDER BY ABS(v.delta_total) DESC LIMIT %s""",
        tuple(params + [limit]),
    )
    units = _unit_names(fiscal_year) if is_exp else {}
    out = []
    for r in rows:
        item = {
            "decision": r["decision_number"],
            "decision_date": str(r["decision_date"]) if r["decision_date"] else None,
            "code": r["code"],
            "name": r["line_name"],
            "delta_total": _f(r["delta_total"]),
            "is_new_program": r["is_new"],
        }
        if is_exp:
            kvk = r["code"][:2]
            item["owner_kvk"] = kvk
            item["owner_name"] = units.get(kvk)
        out.append(item)
    note = ("delta_total у грн; is_new_program=true — програми не було в попередній редакції; "
            "owner_name — розпорядник, який освоюватиме ці гроші (за КВК з КПКВК)")
    if month and not out:
        # порожньо саме через фільтр місяця — з'ясуємо, чи це «немає дат»
        dated = bot_db.query(
            "SELECT count(*) c FROM budget.plan_revision "
            "WHERE fiscal_year = %s AND kind='amendment' AND decision_date IS NOT NULL",
            (fiscal_year,),
        )
        if dated and dated[0]["c"] == 0:
            note = ("У ревізій цього року не проставлені дати ухвалення, тому фільтр по "
                    "місяцю нічого не знайшов. Спитай без місяця (по номеру рішення) або "
                    "задай дати через /budget_date. Список ревізій — query_type='revisions'.")
    return {"fiscal_year": fiscal_year, "kind": kind, "amendments": out, "note": note}


def _line_history(fiscal_year, kind, code, name_contains, limit):
    """Історія рядка по ревізіях: як рішення міняли план програми/доходу."""
    if not code and not name_contains:
        return {"error": "Потрібен code або name_contains"}
    is_exp = kind != "revenue"
    table = "budget.plan_expenditure_line" if is_exp else "budget.plan_revenue_line"
    code_col = "kpkvk" if is_exp else "code"
    where, params = ["r.fiscal_year = %s"], [fiscal_year]
    if code:
        where.append(f"l.{code_col} = %s")
        params.append(_norm_code(code, 7 if is_exp else 8))
    if name_contains:
        where.append("l.line_name ILIKE %s")
        params.append(f"%{name_contains}%")
    rows = bot_db.query(
        f"""SELECT r.effective_order, r.decision_number, r.decision_date,
                   l.{code_col} AS code, l.line_name, l.total
            FROM {table} l
            JOIN budget.plan_revision r ON r.id = l.revision_id
            WHERE {' AND '.join(where)}
            ORDER BY l.{code_col}, r.effective_order
            LIMIT %s""",
        tuple(params + [limit * 6]),  # кілька кодів × кілька ревізій
    )
    units = _unit_names(fiscal_year) if is_exp else {}
    history = {}
    for r in rows:
        h = history.setdefault(r["code"], {
            "code": r["code"], "name": r["line_name"],
            "owner_name": units.get(r["code"][:2]) if is_exp else None,
            "by_revision": [],
        })
        h["by_revision"].append({
            "decision": r["decision_number"],
            "date": str(r["decision_date"]) if r["decision_date"] else None,
            "total": _f(r["total"]),
        })
    return {"fiscal_year": fiscal_year, "kind": kind,
            "lines": list(history.values())[:limit]}


def _revisions(fiscal_year):
    """Реєстр ревізій року з сумами і заголовними показниками."""
    rows = bot_db.query(
        """SELECT r.effective_order, r.decision_number, r.decision_date, r.kind,
                  (SELECT COALESCE(SUM(e.total),0) FROM budget.plan_expenditure_line e
                    WHERE e.revision_id = r.id AND NOT e.is_unit_total) AS exp_total,
                  (SELECT COALESCE(SUM(v.total),0) FROM budget.plan_revenue_line v
                    WHERE v.revision_id = r.id AND v.code ~ '^\\d0{7}$') AS rev_total,
                  (SELECT COUNT(*) FROM budget.revision_validation_issue i
                    WHERE i.revision_id = r.id) AS issues,
                  h.revenue_total AS headline_revenue, h.expenditure_total AS headline_expenditure
           FROM budget.plan_revision r
           LEFT JOIN budget.plan_headline h ON h.revision_id = r.id
           WHERE r.fiscal_year = %s ORDER BY r.effective_order""",
        (fiscal_year,),
    )
    return {"fiscal_year": fiscal_year, "revisions": [{
        "order": r["effective_order"], "decision": r["decision_number"],
        "date": str(r["decision_date"]) if r["decision_date"] else None,
        "kind": r["kind"],
        "expenditure_total": _f(r["exp_total"]) or None,
        "revenue_total": _f(r["rev_total"]) or None,
        "headline_expenditure": _f(r["headline_expenditure"]),
        "headline_revenue": _f(r["headline_revenue"]),
        "validation_issues": r["issues"],
    } for r in rows],
        "note": "expenditure/revenue_total = 0 або null — рішення цю частину не міняло "
                "(успадкована); issues — розбіжності «чинної редакції» (міжсесійні зміни/одруківки)"}


def _execution(fiscal_year, name_contains, limit):
    """Виконання з останнього місячного снапшота: по розпорядниках."""
    snap = bot_db.query(
        "SELECT * FROM budget.snapshot WHERE kind='expenditure' AND fiscal_year = %s "
        "ORDER BY snapshot_date DESC LIMIT 1",
        (fiscal_year,),
    )
    if not snap:
        return {"error": f"Снапшотів виконання за {fiscal_year} ще немає"}
    snap = snap[0]
    where, params = ["snapshot_id = %s", "code_type = 'unit'"], [snap["id"]]
    if name_contains:
        where.append("unit_name ILIKE %s")
        params.append(f"%{name_contains}%")
    units = bot_db.query(
        f"""SELECT kvk, unit_name, annual_plan, period_plan, actual, pct
            FROM budget.snapshot_expenditure_line
            WHERE {' AND '.join(where)} ORDER BY pct NULLS LAST LIMIT %s""",
        tuple(params + [limit]),
    )
    total = bot_db.query(
        "SELECT annual_plan, period_plan, actual, pct FROM budget.snapshot_expenditure_line "
        "WHERE snapshot_id = %s AND code_type = 'total'",
        (snap["id"],),
    )
    return {
        "fiscal_year": fiscal_year,
        "snapshot_date": str(snap["snapshot_date"]),
        "units": [{
            "kvk": u["kvk"], "name": u["unit_name"],
            "annual_plan": _f(u["annual_plan"]), "period_plan": _f(u["period_plan"]),
            "actual": _f(u["actual"]), "pct_of_period_plan": _f(u["pct"]),
        } for u in units],
        "total": {
            "annual_plan": _f(total[0]["annual_plan"]), "period_plan": _f(total[0]["period_plan"]),
            "actual": _f(total[0]["actual"]), "pct_of_period_plan": _f(total[0]["pct"]),
        } if total else None,
        "note": "pct_of_period_plan — % касових видатків до плану ЗВІТНОГО періоду "
                "(хто не виконує — у кого низький %); методологія знімка: без кредитів "
                "і власних надходжень установ, сортування від найгірших",
    }
