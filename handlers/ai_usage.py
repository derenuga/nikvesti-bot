"""
Звіт про вартість AI-шару Лиса (REVIEW п. в.5).

Кожен AI-виклик (fox_generate у ai_messages + tool-use цикл NLQ у query_router)
пише токени в місячний агрегат storage (record_ai_usage). Тут — оцінка вартості
за прайсом моделей і форматування звіту.

- /aicost — витрати за поточний місяць на вимогу
- 1-го числа щомісяця Лис сам звітує Олегу за попередній місяць (scheduler)

Ціни приблизні (стандартний прайс $/1M токенів); кеш-читання ~0.1× input,
кеш-запис ~1.25× input. Оцінка «згори» — під час інтро-цін Sonnet 5
реальні витрати трохи нижчі.
"""

from datetime import datetime, timedelta

from handlers import storage

# $ за 1M токенів (input, output)
PRICING = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}
DEFAULT_PRICE = (3.0, 15.0)

# Людські назви інструментів для розрізу «на що йшли гроші». Ключ ставить сам
# виклик (record_ai_usage(feature=…)); незнайомий ключ друкується як є, тож
# новий інструмент з'являється у звіті сам, а сюди дописують лише назву.
FEATURE_TITLES = {
    "carousel": "Каруселі для Instagram",
}


def _model_cost(model, rec):
    price_in, price_out = PRICING.get(model, DEFAULT_PRICE)
    return (
        rec.get("input", 0) / 1e6 * price_in
        + rec.get("output", 0) / 1e6 * price_out
        + rec.get("cache_read", 0) / 1e6 * price_in * 0.1
        + rec.get("cache_creation", 0) / 1e6 * price_in * 1.25
    )


def models_cost(models):
    """Вартість набору {model: rec} за прайсом — спільна лінійка для місячного
    розрізу по людях (/aicost) і денного зрізу (/usage), щоб числа сходились."""
    return sum(_model_cost(model, rec) for model, rec in (models or {}).items())


def models_tokens(models):
    """Сумарні токени набору {model: rec} (input+output+кеш)."""
    return sum(
        rec.get("input", 0) + rec.get("output", 0)
        + rec.get("cache_read", 0) + rec.get("cache_creation", 0)
        for rec in (models or {}).values()
    )


def fmt_cost(cost):
    """«~$0.34», а для копійчаних ненульових — «<$0.01», не оманливе «$0.00»."""
    if 0 < cost < 0.005:
        return "<$0.01"
    return f"~${cost:.2f}"


def format_month_report(month):
    """Текст звіту за місяць 'YYYY-MM'. None якщо витрат не було."""
    usage = storage.get_ai_usage(month)
    if not usage:
        return None

    lines = [f"🦊 Скільки я коштував за {month}:", ""]
    total_cost = 0.0
    total_requests = 0
    total_tokens = 0
    for model, rec in sorted(usage.items()):
        cost = _model_cost(model, rec)
        total_cost += cost
        total_requests += rec.get("requests", 0)
        model_tokens = rec.get("input", 0) + rec.get("output", 0) + rec.get("cache_read", 0) + rec.get("cache_creation", 0)
        total_tokens += model_tokens
        short = model.replace("claude-", "")
        lines.append(f"  {short}: {rec.get('requests', 0)} звернень, ~${cost:.2f}")

    lines.append("")
    lines.append(f"Разом: {total_requests} звернень до AI, {total_tokens // 1000} тис. токенів, ~${total_cost:.2f}")

    # Розріз ПО ІНСТРУМЕНТАХ — відповідає на «скільки з'їли каруселі», чого
    # розріз по людях сказати не може: той самий Лис у тієї самої людини і на
    # питання відповідає, і слайди складає. Позначку ставить сам виклик, тому
    # тут лише те, що вже позначене, — і про це сказано вголос, інакше «решта»
    # читалася б як «більше ні на що не витрачали».
    by_feature = storage.get_ai_usage_features(month)
    if by_feature:
        rows = []
        tagged_cost = 0.0
        for key, models in by_feature.items():
            cost = models_cost(models)
            tagged_cost += cost
            requests = sum(m.get("requests", 0) for m in models.values())
            rows.append((cost, requests, FEATURE_TITLES.get(key, key)))
        lines.append("")
        lines.append("На що йшли гроші:")
        for cost, requests, title in sorted(rows, key=lambda r: -r[0]):
            lines.append(f"  {title}: {requests} звернень, {fmt_cost(cost)}")
        rest = total_cost - tagged_cost
        if rest > 0.005:
            lines.append(f"  Решта Лиса (ще без розрізу): ~${rest:.2f}")

    # Розріз по людях: тільки виклики, які ініціювала конкретна людина
    # (NLQ-питання, беки, /dossier). Решта — автоматика Лиса за розкладом.
    by_user = storage.get_ai_usage_users(month)
    if by_user:
        rows = []
        users_cost = 0.0
        for uid, user_rec in by_user.items():
            models = user_rec.get("models", {})
            cost = models_cost(models)
            users_cost += cost
            requests = sum(m.get("requests", 0) for m in models.values())
            rows.append((cost, requests, user_rec.get("name") or f"id {uid}"))
        lines.append("")
        lines.append("Хто скільки напитав (NLQ, беки, досьє):")
        for cost, requests, name in sorted(rows, key=lambda r: -r[0]):
            lines.append(f"  {name}: {requests} звернень, {fmt_cost(cost)}")
        auto_cost = total_cost - users_cost
        if auto_cost > 0.005:
            lines.append(f"  Автоматика Лиса (ранкове, звіти, судді, витяги): ~${auto_cost:.2f}")

    lines.append("(оцінка згори; облік запущено з липня 2026, розріз по людях — із 14.08.2026)")
    return "\n".join(lines)


async def send_monthly_ai_cost(bot, chat_id):
    """Звіт за ПОПЕРЕДНІЙ місяць — для щомісячного автозапуску 1-го числа."""
    prev_month_last = datetime.now().replace(day=1) - timedelta(days=1)
    month = prev_month_last.strftime("%Y-%m")
    text = format_month_report(month)
    if text:
        await bot.send_message(chat_id=chat_id, text=text)


async def aicost_handler(update, context):
    """/aicost — витрати AI за поточний місяць (можна вказати YYYY-MM)."""
    month = datetime.now().strftime("%Y-%m")
    if context.args and len(context.args[0]) == 7:
        month = context.args[0]
    text = format_month_report(month)
    await update.message.reply_text(text or f"За {month} витрат AI ще немає.")
