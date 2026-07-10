-- Сутнісний шар над «лисячою норою» (крок C плану docs/ENTITY_LAYER_PLAN.md, §3.2).
-- Ідемпотентно: безпечно застосовувати повторно (IF NOT EXISTS).
-- Застосувати до Postgres бота (нора). Патерн — tags/article_tags.
--
-- Застосування:
--   psql "$DATABASE_PUBLIC_URL" -f entity_schema.sql
-- або з прямого-інтернет термінала (Варіант 2):
--   psql "postgresql://postgres:...@reseau.proxy.rlwy.net:46884/railway" -f entity_schema.sql
--
-- Звірка після застосування:
--   SELECT to_regclass('entities'), to_regclass('article_entities');   -- обидва НЕ NULL
--   SELECT value FROM sync_state WHERE key = 'entity_last_id';           -- курсор = '0'

-- entities — картки реальних речей (одна на людину/організацію/місце/документ/подію)
CREATE TABLE IF NOT EXISTS entities (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,          -- person | org | place | document | event
    subtype     TEXT,                   -- деталізація в межах kind (gov/company/facility/decision…)
    name_ua     TEXT,
    name_ru     TEXT,
    aliases     TEXT[] DEFAULT '{}',    -- варіанти написання, що траплялись
    role_last   TEXT,                   -- остання відома роль (для показу)
    first_seen  BIGINT,                 -- unix, найраніша згадка
    last_seen   BIGINT,                 -- unix, найпізніша згадка
    mentions    INT DEFAULT 0           -- лічильник статей
);

-- Нормалізоване ім'я в межах kind — ключ точного злиття (див. §3.3, однофамільці).
-- Тримаємо як звичайний індекс (не UNIQUE): злиття робить оркестратор свідомо,
-- сумнівних тезок лишає окремими сутностями.
CREATE INDEX IF NOT EXISTS idx_entities_kind        ON entities (kind);
CREATE INDEX IF NOT EXISTS idx_entities_kind_nameua ON entities (kind, lower(name_ua));
CREATE INDEX IF NOT EXISTS idx_entities_kind_nameru ON entities (kind, lower(name_ru));

-- article_entities — зв'язки стаття↔сутність, з роллю на момент і центральністю
CREATE TABLE IF NOT EXISTS article_entities (
    article_id   BIGINT NOT NULL,
    entity_id    BIGINT NOT NULL,
    role_at_time TEXT,                  -- роль на момент статті («депутат міськради»)
    salience     TEXT,                  -- main | mentioned
    PRIMARY KEY (article_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities (entity_id);

-- Курсор витягу (resumable, патерн бекфілу архіву): наступна сесія продовжує з місця.
INSERT INTO sync_state (key, value)
VALUES ('entity_last_id', '0')
ON CONFLICT (key) DO NOTHING;
