-- Роль ТІЛЬКИ ДЛЯ ЧИТАННЯ неперсональної частини нори.
--
-- ЯК ЗАПУСТИТИ: Railway → сервіс Postgres → вкладка **Console**. Замінити
-- пароль, вставити ВЕСЬ файл, виконати. Повторний запуск безпечний: роль не
-- дублюється, пароль просто оновиться.
--
-- НЕ у вкладці Database → Data: те поле з підказкою «SELECT * FROM
-- sync_state» — це переглядач таблиць, він дописує до запиту власний LIMIT
-- і падає з «syntax error at or near LIMIT» одразу після END $$ (Олег,
-- 16.08). Для команд там місця немає, лише для SELECT.
--
-- Файл лишається ОДНИМ оператором (DO-блок) свідомо: так його приймає
-- будь-яке вікно, а не лише повноцінна консоль.
--
-- НАВІЩО. До Claude підключається MCP-сервер із доступом до нори по HTTPS
-- (пісочниця сесії випускає лише 443, прямого TCP до Postgres немає). Це
-- зручно, але за публічним адресом і одним токеном — тому токен має бачити
-- рівно те, що не є персональними даними.
--
-- Кнопка «Connect» у панелі тут НЕ ПІДХОДИТЬ: вона дає DATABASE_URL, тобто
-- адмінський доступ до всього. Саме від цього ми й відмовляємось.
--
-- ЩО ВИДНО: робота — архів, сутності, канон ролей, банк тем, метрики,
-- завдання, проєкти й зараховані до них публікації, бюджет міста.
-- ЩО НЕ ВИДНО: людина — телефонна база редакції (team_contacts), квартальні
-- оцінки (team_quality_reviews), журнал перегляду умов (team_condition_
-- reviews), ставки (team_rates), відпустки й ЛІКАРНЯНІ (team_absences),
-- особистий блокнот (team_todos), правки KPI з нотатками (team_kpi_overrides),
-- стрічка подій (team_notifications), чернетки донорських кейсів (impacts).
--
-- Логіка та сама, що в рішенні Олега 11.08 не тримати в норі суми по
-- зарплатах: «Нора наполовину стає зарплатним реєстром, а вона відкривається
-- з телеграма». Публічний HTTPS — крок ДАЛІ за телеграм. Розширити потім
-- легко, звузити після витоку — неможливо.

DO $$
DECLARE
  -- ⬇⬇⬇ ЗАМІНИТИ НА СВІЙ ПАРОЛЬ (той самий піде в DATABASE_URI сервісу MCP)
  pw text := 'ЗАМІНИ_ЦЕЙ_ПАРОЛЬ';
  t   text;
  -- Таблиці, які роль бачить. Немає в базі — рядок просто пропускається,
  -- тож файл не падає на свіжій норі, де щось ще не створено.
  allowed text[] := ARRAY[
    -- дзеркало архіву й повнотекстовий пошук
    'articles', 'article_tags', 'tags',
    -- сутнісний шар і канон ролей
    'entities', 'article_entities', 'entity_attempts',
    'entity_merges', 'entity_merge_rules',
    'role_canon', 'role_variants', 'role_pairs', 'v_entity_roles',
    -- банк тем
    'commitments', 'commitment_revisions', 'commitment_objects',
    'topics', 'topic_merges', 'topic_pair_verdicts',
    'promise_pairs', 'promise_pair_verdicts', 'promise_closures',
    'promise_stars', 'promise_purges', 'promise_attempts', 'promise_reminders',
    -- метрики (публічні або службові числа)
    'daily_stats', 'sc_daily_stats', 'social_stats', 'social_monthly',
    'article_stats',
    -- завдання, проєкти й зараховані публікації (для звітів донорам)
    'team_creative_tasks', 'team_task_matches', 'team_task_events',
    'team_project_themes', 'team_project_order', 'team_project_deadlines',
    'team_project_drive', 'team_deadline_reminders', 'team_user_link',
    -- службовий стан синків (курсори, без вмісту)
    'sync_state'
  ];
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nora_reader') THEN
    EXECUTE format('ALTER ROLE nora_reader LOGIN PASSWORD %L', pw);
  ELSE
    EXECUTE format('CREATE ROLE nora_reader LOGIN PASSWORD %L', pw);
  END IF;

  EXECUTE format('GRANT CONNECT ON DATABASE %I TO nora_reader', current_database());
  EXECUTE 'GRANT USAGE ON SCHEMA public TO nora_reader';

  FOREACH t IN ARRAY allowed LOOP
    IF to_regclass('public.' || quote_ident(t)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON public.%I TO nora_reader', t);
    END IF;
  END LOOP;

  -- Бюджет міста: окрема схема, дані офіційно публічні
  IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'budget') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA budget TO nora_reader';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA budget TO nora_reader';
  END IF;

  RAISE NOTICE 'nora_reader готова';
END $$;

-- ПЕРЕВІРКА (запустити окремо після першого блоку).
-- У списку не має бути жодного рядка — усе зайве відсіяно заздалегідь.
-- Якщо щось із цього зʼявиться — доступ ширший, ніж домовлялись.
SELECT table_schema, table_name
FROM information_schema.table_privileges
WHERE grantee = 'nora_reader'
  AND (table_name IN ('team_contacts', 'team_quality_reviews',
                      'team_condition_reviews', 'team_rates', 'team_absences',
                      'team_absence_requests', 'team_todos',
                      'team_kpi_norms', 'team_kpi_overrides',
                      'team_notifications', 'team_notification_reads',
                      'impacts', 'impact_articles', 'impact_credits'));
