-- Роль ТІЛЬКИ ДЛЯ ЧИТАННЯ неперсональної частини нори.
--
-- Навіщо. До Claude підключається MCP-сервер із доступом до нори по HTTPS
-- (пісочниця сесії випускає лише 443, тому прямого TCP до Postgres немає).
-- Це зручно — архів, сутності, банк тем і метрики стають доступні прямо в
-- сесії, — але за публічним адресом і одним токеном. Тому токен має бачити
-- рівно те, що не є персональними даними.
--
-- Що НЕ віддається і чому. У норі лежить не лише дзеркало сайту:
--   team_contacts          — телефонна база редакції: номери, теми, контакти
--                            (для журналістики це фактично контакти джерел)
--   team_quality_reviews   — квартальні оцінки редакторки по людях
--   team_condition_reviews — журнал перегляду умов, з контекстом словами
--   team_rates             — постійна ставка людини у відсотках
--   team_absences          — відпустки, відрядження і ЛІКАРНЯНІ (здоров'я)
--   team_todos             — особистий блокнот («приватний повністю»)
--   team_kpi_overrides     — правки цілей із нотатками про людину
--   team_notifications     — стрічка подій конкретних людей
--   impacts / impact_*     — чернетки донорських кейсів до показу донору
-- Межа проста: РОБОТА (завдання, проєкти, публікації) видима, ЛЮДИНА
-- (здоров'я, гроші, оцінки, приватні записи, контакти) — ні.
-- Логіка та сама, що в рішенні Олега 11.08 не тримати в норі суми й тип
-- рішення по зарплатах: «Нора наполовину стає зарплатним реєстром, а вона
-- відкривається з телеграма». Публічний HTTPS — крок ДАЛІ за телеграм.
--
-- Розширити доступ потім — це один рядок GRANT. Звузити після витоку —
-- неможливо, тому починаємо вужче.
--
-- Виконати РАЗ у панелі Railway (Postgres → Data/Query) під адмінським
-- користувачем. Пароль підставити свій, той самий піде в DATABASE_URI
-- сервісу MCP.

-- ── роль ────────────────────────────────────────────────────────────────
CREATE ROLE nora_reader LOGIN PASSWORD 'ЗАМІНИТИ_НА_СВІЙ_ПАРОЛЬ';

GRANT CONNECT ON DATABASE railway TO nora_reader;
GRANT USAGE ON SCHEMA public TO nora_reader;

-- ── дзеркало архіву і повнотекстовий пошук ──────────────────────────────
GRANT SELECT ON articles, article_tags, tags TO nora_reader;

-- ── сутнісний шар і канон ролей ─────────────────────────────────────────
GRANT SELECT ON entities, article_entities, entity_attempts,
                entity_merges, entity_merge_rules,
                role_canon, role_variants, role_pairs TO nora_reader;

-- ── банк тем ────────────────────────────────────────────────────────────
GRANT SELECT ON commitments, commitment_revisions, commitment_objects,
                topics, topic_merges, topic_pair_verdicts,
                promise_pairs, promise_pair_verdicts, promise_closures,
                promise_stars, promise_purges, promise_attempts,
                promise_reminders TO nora_reader;

-- ── метрики (усе це й так публічне або службове) ────────────────────────
GRANT SELECT ON daily_stats, sc_daily_stats, social_stats, social_monthly,
                article_stats TO nora_reader;

-- ── службовий стан синків (курсори, без вмісту) ─────────────────────────
GRANT SELECT ON sync_state TO nora_reader;

-- ── завдання, проєкти й зараховані до них публікації ────────────────────
-- Олег, 16.08: «вдруг я через это буду отчеты свои делать». Це РОБОТА, а не
-- дані про людину: що замовили, в який проєкт, з яким строком і що зрештою
-- вийшло. Оцінки, ставки, лікарняні й контакти лишаються поза доступом.
--
-- Межа даних: самі ПРОЄКТИ І ДОНОРИ живуть не тут, а в MySQL сайту
-- (partner_project / partner). У норі лежить лише надбудова над ними —
-- тематики, порядок, звітні дедлайни, папка Drive — і посилання project_id.
-- Тобто через MCP видно «що зроблено по проєкту», але назву донора доведеться
-- брати з сайту (у боті це /dbquery, в апці — team_projects.py).
GRANT SELECT ON team_creative_tasks, team_task_matches, team_task_events,
                team_project_themes, team_project_order,
                team_project_deadlines, team_project_drive,
                team_deadline_reminders, team_user_link TO nora_reader;

-- ── бюджет міста: окрема схема, дані офіційно публічні ───────────────────
GRANT USAGE ON SCHEMA budget TO nora_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA budget TO nora_reader;

-- ── в'юхи, якими користуються ті самі запити ────────────────────────────
-- (v_entity_roles: сира роль + канон поруч; v_plan_amendments: дельти бюджету)
DO $$
BEGIN
  IF to_regclass('public.v_entity_roles') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT ON public.v_entity_roles TO nora_reader';
  END IF;
  IF to_regclass('budget.v_plan_amendments') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT ON budget.v_plan_amendments TO nora_reader';
  END IF;
END $$;

-- ── перевірка: що роль реально бачить ───────────────────────────────────
-- Має бути список БЕЗ жодної team_* і без impacts.
SELECT table_schema, table_name
FROM information_schema.table_privileges
WHERE grantee = 'nora_reader' AND privilege_type = 'SELECT'
ORDER BY table_schema, table_name;
