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


def _model_cost(model, rec):
    price_in, price_out = PRICING.get(model, DEFAULT_PRICE)
    return (
        rec.get("input", 0) / 1e6 * price_in
        + rec.get("output", 0) / 1e6 * price_out
        + rec.get("cache_read", 0) / 1e6 * price_in * 0.1
        + rec.get("cache_creation", 0) / 1e6 * price_in * 1.25
    )


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
    lines.append("(оцінка згори; облік запущено з липня 2026)")
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
