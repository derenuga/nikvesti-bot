-- Сутнісний шар над «лисячою норою» — схема за docs/ENTITY_LAYER_PLAN.md §3.2.
-- Крок 1 тесту сутнісного шару (крок C, шлях Б). Ідемпотентно (IF NOT EXISTS):
-- застосувати раз до нори (Postgres бота). Після валідації якості тесту (§3.6)
-- ця схема переїде у handlers/bot_db.py ensure_schema (штатний патерн міграцій).

-- Картки реальних речей: одна на людину/організацію/місце/документ/подію.
CREATE TABLE IF NOT EXISTS entities (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,            -- person | org | place | document | event
    subtype    TEXT,                     -- деталізація в межах kind (gov/company/facility/decision…)
    name_ua    TEXT,
    name_ru    TEXT,
    aliases    TEXT[],                   -- варіанти написання, що траплялись
    role_last  TEXT,                     -- остання відома роль (для показу)
    first_seen BIGINT,                   -- unix, найраніша згадка
    last_seen  BIGINT,                   -- unix, найпізніша згадка
    mentions   INT NOT NULL DEFAULT 0    -- лічильник статей
);

-- Індекси під точне злиття по нормалізованому імені в межах kind.
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities (kind);
CREATE INDEX IF NOT EXISTS idx_entities_name_ua ON entities (lower(name_ua));
CREATE INDEX IF NOT EXISTS idx_entities_name_ru ON entities (lower(name_ru));

-- Звʼязок стаття↔сутність, з роллю на момент і центральністю.
CREATE TABLE IF NOT EXISTS article_entities (
    article_id   BIGINT NOT NULL,
    entity_id    BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role_at_time TEXT,                   -- роль на момент статті («депутат міськради»)
    salience     TEXT,                   -- main | mentioned
    PRIMARY KEY (article_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities (entity_id);

-- Курсор resumable-прогону (той самий патерн, що backfill архіву).
INSERT INTO sync_state (key, value) VALUES ('entity_last_id', '0')
ON CONFLICT (key) DO NOTHING;
