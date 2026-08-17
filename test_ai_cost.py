"""
Тест прайсу AI-звіту: вступні ціни рахуються за ПЕРІОДОМ звіту.

Навіщо. Sonnet 5 до 31.08.2026 коштує $2/$10 замість $3/$15, а звіт рахував
усе за постійним прайсом — і приписував нам зайву третину по Sonnet ($26.15
там, де ~$17.5). Помилка тиха: числа виглядають нормально, просто вони не ті.

Тест стереже дві речі, які легко зламати різними способами:
  1. межу дати — серпень за вступним прайсом, вересень за звичайним
     (щоб 1 вересня прайс повернувся сам, а не тоді, коли хтось помітить);
  2. що звіт за минулий період не «пливе» від сьогоднішньої дати — інакше
     серпневий звіт, відкритий у грудні, показав би інші гроші.

Дати тут вписані числом свідомо, і це НЕ порушення правила про міни: вони
описують не «зараз», а межу, оголошену Anthropic. Міна — це `сьогодні`,
вписане числом; тут же фіксується історичний факт.

Запуск: python test_ai_cost.py
"""

import sys

from handlers import ai_usage


def main():
    results = []

    def check(ok, title, detail=""):
        results.append(ok)
        print(f"  {'✅' if ok else '❌'} {title}{'' if ok else f' — {detail}'}")

    print("Прайс AI-звіту:")

    # --- межа вступної ціни ---
    aug = ai_usage.price_for("claude-sonnet-5", "2026-08")
    check(aug == (2.0, 10.0), "серпень 2026 — вступна ціна Sonnet 5", f"віддало {aug}")

    last = ai_usage.price_for("claude-sonnet-5", "2026-08-31")
    check(last == (2.0, 10.0), "31 серпня — ще вступна (межа включна)", f"віддало {last}")

    first = ai_usage.price_for("claude-sonnet-5", "2026-09-01")
    check(first == (3.0, 15.0), "1 вересня — вже звичайна, сама", f"віддало {first}")

    sep = ai_usage.price_for("claude-sonnet-5", "2026-09")
    check(sep == (3.0, 15.0), "вересень 2026 — звичайна", f"віддало {sep}")

    # --- моделі без вступної ціни не зачіпає ---
    haiku = ai_usage.price_for("claude-haiku-4-5", "2026-08")
    check(haiku == (1.0, 5.0), "Haiku вступної ціни не має — прайс не змінився",
          f"віддало {haiku}")

    # --- без періоду помиляємось у бік «дорожче» ---
    unknown = ai_usage.price_for("claude-sonnet-5")
    check(unknown == (3.0, 15.0), "без періоду — звичайна ціна (оцінка згори)",
          f"віддало {unknown}")

    # --- рахунок за набором ---
    models = {"claude-sonnet-5": {"input": 1_000_000, "output": 100_000}}
    aug_cost = ai_usage.models_cost(models, "2026-08")
    sep_cost = ai_usage.models_cost(models, "2026-09")
    check(abs(aug_cost - 3.0) < 1e-9, "1М input + 100к output у серпні = $3.00",
          f"віддало {aug_cost}")
    check(abs(sep_cost - 4.5) < 1e-9, "той самий обсяг у вересні = $4.50",
          f"віддало {sep_cost}")
    check(aug_cost < sep_cost, "серпень дешевший за вересень, а не навпаки")

    # --- кеш рахується від ціни ТОГО САМОГО періоду ---
    cached = {"claude-sonnet-5": {"cache_read": 10_000_000}}
    aug_cache = ai_usage.models_cost(cached, "2026-08")
    check(abs(aug_cache - 2.0) < 1e-9,
          "10М читання з кешу в серпні = $2.00 (0.1× від вступних $2)",
          f"віддало {aug_cache}")

    bad = len([r for r in results if not r])
    print(f"\n{'❌ Провалено: %d із %d' % (bad, len(results)) if bad else '✅ Усі %d перевірки пройшли' % len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
