/**
 * Інструменти, які сервер віддає Клоду.
 *
 * НАЗВАНІ інструменти, а не голий SQL, — свідомо. Сирий `execute_sql` вимагає
 * знати схему нори напам'ять і кожного разу писати запит заново; названий
 * інструмент відповідає на питання людини («що вийшло по IWPR у липні»)
 * і повертає готові рядки з лінками. Голий SELECT лишається останнім
 * інструментом як запасний вихід — але саме запасний, а не єдиний.
 *
 * Усі запити йдуть у пул `nora` (роль nora_reader): тільки читання і лише
 * неперсональна частина бази. Це не домовленість, а права в самій базі.
 */
import { z } from "zod"
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import type { AuthInfo } from "@modelcontextprotocol/sdk/server/auth/types.js"

import { nora } from "./db.js"
import { env } from "./env.js"

const BASE_URL = "https://nikvesti.com"

const text = (value: string) => ({ content: [{ type: "text" as const, text: value }] })

/** Дата з unix-секунд у людський вигляд. */
function fmtDate(seconds: number | string | null): string {
  const n = Number(seconds)
  if (!n) return "—"
  return new Date(n * 1000).toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit", year: "numeric" })
}

/**
 * Лінк на матеріал. Правило те саме, що в handlers/archive_search.py: статті
 * живуть на /articles/, новини — на /news/{рубрика}/, а старі матеріали бувають
 * без слага, і тоді хвостом іде id. Рубрику вставляємо завжди, інакше двіжок
 * редиректить і в тексті плодяться голі лінки.
 */
function articleUrl(row: { id: string; slug: string | null; category: string | null; kind: string | null }): string {
  const tail = (row.slug || "").trim() || String(row.id)
  if ((row.kind || "news") === "article") return `${BASE_URL}/articles/${tail}`
  const category = (row.category || "").trim()
  return category ? `${BASE_URL}/news/${category}/${tail}` : `${BASE_URL}/news/${tail}`
}

/**
 * Слова запиту → вираз для to_tsquery('simple', …): 'основ:* & друг:*'.
 * Префіксний пошук обов'язковий: українська відмінює все, і «Сєнкевича» без
 * зірочки не знайде «Сєнкевич».
 */
function tsquery(query: string): string {
  const words = query
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((w) => w.length >= 3)
    .slice(0, 8)
  if (words.length === 0) return ""
  return words.map((w) => `${w}:*`).join(" & ")
}

export function buildServer(auth: AuthInfo | undefined): McpServer {
  const server = new McpServer(
    { name: env.serverName, version: "1.0.0" },
    {
      capabilities: { tools: {} },
      instructions:
        "Нора МикВісті — 17-річний архів новин, сутнісний шар, банк тем влади та облік " +
        "завдань редакції. Доступ лише на читання і лише до неперсональної частини: " +
        "телефонної бази, оцінок людей, лікарняних і особистих записів тут немає й не буде.",
    },
  )

  server.registerTool(
    "search_archive",
    {
      title: "Пошук по архіву",
      description:
        "Повнотекстовий пошук по 17-річному архіву новин і статей МикВісті. " +
        "Повертає дату, заголовок, лінк і РЕЧЕННЯ З ТЕКСТУ навколо збігу. " +
        "Для «свіжого» задавай date_from — на вічних темах (обстріли, ремонти доріг) " +
        "релевантний збіг не означає свіжий.",
      inputSchema: {
        query: z.string().min(2).max(200).describe("слова для пошуку, українською або російською"),
        date_from: z.string().optional().describe("не раніше цієї дати, YYYY-MM-DD"),
        date_to: z.string().optional().describe("не пізніше цієї дати, YYYY-MM-DD"),
        own_only: z.boolean().optional().describe("лише власні матеріали редакції"),
        limit: z.number().int().min(1).max(50).optional(),
      },
    },
    async ({ query, date_from, date_to, own_only, limit }) => {
      const q = tsquery(query)
      if (!q) return text("Замало слів для пошуку — потрібне хоча б одне слово з трьох літер.")
      const { rows } = await nora.query(
        `WITH q AS (SELECT to_tsquery('simple', $1) AS query),
              hits AS (
                SELECT a.id::text, a.published, a.title_ua, a.title_ru, a.slug, a.category, a.kind,
                       a.own_material, ts_rank(a.fts, q.query) AS rank
                  FROM articles a, q
                 WHERE a.fts @@ q.query
                   AND ($2::bigint IS NULL OR a.published >= $2)
                   AND ($3::bigint IS NULL OR a.published <= $3)
                   AND ($4::boolean IS NOT TRUE OR a.own_material = 1)
                 ORDER BY a.published DESC
                 LIMIT $5
              )
         SELECT h.*, ts_headline('simple',
                  left(coalesce(nullif(a2.text_ua, ''), a2.text_ru, ''), 20000),
                  q.query,
                  -- Маркери виділення ПОРОЖНІ й обовʼязково в лапках: без них
                  -- Postgres читає «StartSel=,StopSel=» як одне значення і
                  -- лишає в тексті свій дефолтний <b>, а цитата з тегами
                  -- всередині їде далі в бек (той самий рядок у
                  -- handlers/archive_search.py має лапки саме тому).
                  'StartSel="", StopSel="", MaxWords=45, MinWords=20, MaxFragments=1') AS excerpt
           FROM hits h JOIN articles a2 ON a2.id = h.id::bigint, q`,
        [
          q,
          date_from ? Math.floor(new Date(date_from).getTime() / 1000) : null,
          date_to ? Math.floor(new Date(date_to).getTime() / 1000) + 86_399 : null,
          own_only ?? null,
          limit ?? 10,
        ],
      )
      if (rows.length === 0) return text(`За запитом «${query}» в норі нічого немає.`)
      const lines = rows.map((r: any) => {
        const title = (r.title_ua || r.title_ru || "").trim()
        const own = r.own_material === 1 ? " · власний" : ""
        const excerpt = (r.excerpt || "").replace(/\s+/g, " ").trim()
        return `${fmtDate(r.published)}${own} — ${title}\n${articleUrl(r)}${excerpt ? `\n…${excerpt}…` : ""}`
      })
      return text(`Знайдено ${rows.length}:\n\n${lines.join("\n\n")}`)
    },
  )

  server.registerTool(
    "article",
    {
      title: "Матеріал повністю",
      description:
        "Повний текст матеріалу з нори за id або URL. Рятує і тоді, коли на сайті " +
        "матеріал затерли або видалили — у норі лишається знімок.",
      inputSchema: { id_or_url: z.string().min(1).max(300) },
    },
    async ({ id_or_url }) => {
      const digits = (id_or_url.match(/(\d{4,})/) || [])[1]
      const { rows } = await nora.query(
        digits
          ? `SELECT id::text, published, title_ua, title_ru, slug, category, kind, own_material,
                    text_ua, text_ru, tags_text, owner_id
               FROM articles WHERE id = $1`
          : `SELECT id::text, published, title_ua, title_ru, slug, category, kind, own_material,
                    text_ua, text_ru, tags_text, owner_id
               FROM articles WHERE slug = $1 LIMIT 1`,
        [digits ? Number(digits) : id_or_url.replace(/^.*\//, "")],
      )
      if (rows.length === 0) return text(`Матеріалу «${id_or_url}» в норі немає.`)
      const r: any = rows[0]
      const body = (r.text_ua || r.text_ru || "").trim()
      return text(
        `${(r.title_ua || r.title_ru || "").trim()}\n${fmtDate(r.published)} · ${articleUrl(r)}` +
          `${r.tags_text ? `\nТеги: ${r.tags_text}` : ""}\n\n${body || "(тексту в норі немає)"}`,
      )
    },
  )

  server.registerTool(
    "promises",
    {
      title: "Банк тем",
      description:
        "Обіцянки влади з банку тем: що обіцяли, хто, до якого строку і в якому стані. " +
        "Фільтр overdue — строк минув, soon — спливає найближчим тижнем, " +
        "checked — вже перевіряли, all — усе поспіль.",
      inputSchema: {
        filter: z.enum(["overdue", "soon", "checked", "all"]).optional(),
        search: z.string().max(120).optional().describe("слово в назві або в обіцяльнику"),
        limit: z.number().int().min(1).max(50).optional(),
      },
    },
    async ({ filter, search, limit }) => {
      const now = Math.floor(Date.now() / 1000)
      const where: string[] = ["1=1"]
      const args: any[] = []
      if (filter === "overdue") where.push(`c.deadline IS NOT NULL AND c.deadline < ${now} AND c.status <> 'done'`)
      if (filter === "soon") where.push(`c.deadline BETWEEN ${now} AND ${now + 7 * 86_400}`)
      if (filter === "checked") where.push(`c.checked_at IS NOT NULL`)
      if (search) {
        args.push(`%${search}%`)
        where.push(`(c.title ILIKE $${args.length} OR c.owner_text ILIKE $${args.length})`)
      }
      args.push(limit ?? 15)
      const { rows } = await nora.query(
        `SELECT c.id::text, c.title, c.owner_text, c.deadline, c.status, c.modality,
                c.revisions, c.checked_at
           FROM commitments c
          WHERE ${where.join(" AND ")}
          ORDER BY (c.deadline IS NULL), c.deadline
          LIMIT $${args.length}`,
        args,
      )
      if (rows.length === 0) return text("За такими умовами в банку тем порожньо.")
      const lines = rows.map((r: any) => {
        const due = r.deadline ? `строк ${fmtDate(r.deadline)}` : "без строку"
        const checked = r.checked_at ? ` · перевіряли ${fmtDate(r.checked_at)}` : ""
        return `#${r.id} ${r.title}\n  ${r.owner_text || "хто обіцяв — не вказано"} · ${due} · ${r.status}${checked}`
      })
      return text(`${rows.length} записів:\n\n${lines.join("\n\n")}`)
    },
  )

  server.registerTool(
    "project_output",
    {
      title: "Вихід по проєкту",
      description:
        "Що зроблено по грантовому проєкту: завдання, тематики й ЗАРАХОВАНІ публікації " +
        "з лінками. Для звіту донору. Без project_id показує список проєктів із числами.",
      inputSchema: {
        project_id: z.number().int().optional(),
        month: z.string().optional().describe("місяць у форматі YYYY-MM; без нього — весь час"),
      },
    },
    async ({ project_id, month }) => {
      if (!project_id) {
        const { rows } = await nora.query(
          `SELECT t.project_id::text, coalesce(max(t.partner_name), '—') AS partner,
                  coalesce(max(t.project_name), '—') AS name, count(*)::text AS tasks
             FROM team_creative_tasks t
            WHERE t.project_id IS NOT NULL
            GROUP BY t.project_id ORDER BY count(*) DESC LIMIT 30`,
        )
        if (rows.length === 0) return text("Проєктних завдань у норі ще немає.")
        return text(
          "Проєкти (id — донор — назва — завдань):\n" +
            rows.map((r: any) => `${r.project_id} — ${r.partner} — ${r.name} — ${r.tasks}`).join("\n"),
        )
      }
      const from = month ? `${month}-01` : null
      const { rows: matches } = await nora.query(
        `SELECT m.title, m.url, m.published, m.person, m.status,
                coalesce(t.theme_name, 'Поза тематиками') AS theme
           FROM team_task_matches m
           LEFT JOIN team_creative_tasks t ON t.id = m.task_id
          WHERE m.project_id = $1
            AND m.status IN ('auto', 'confirmed')
            AND ($2::date IS NULL OR (m.published >= $2::date AND m.published < ($2::date + interval '1 month')))
          ORDER BY m.published DESC LIMIT 200`,
        [project_id, from],
      )
      const { rows: tasks } = await nora.query(
        `SELECT status, count(*)::text AS n FROM team_creative_tasks
          WHERE project_id = $1 GROUP BY status`,
        [project_id],
      )
      const head =
        `Проєкт ${project_id}${month ? `, ${month}` : ""}: ` +
        (tasks.map((t: any) => `${t.status} ${t.n}`).join(" · ") || "завдань немає")
      if (matches.length === 0) return text(`${head}\n\nЗарахованих публікацій за цей період немає.`)
      const byTheme = new Map<string, string[]>()
      for (const m of matches as any[]) {
        const line = `  ${fmtDate(Math.floor(new Date(m.published).getTime() / 1000))} — ${m.title}\n  ${m.url}${m.person ? ` · ${m.person}` : ""}`
        byTheme.set(m.theme, [...(byTheme.get(m.theme) || []), line])
      }
      const blocks = [...byTheme.entries()].map(([theme, lines]) => `${theme} (${lines.length}):\n${lines.join("\n")}`)
      return text(`${head}\nЗараховано публікацій: ${matches.length}\n\n${blocks.join("\n\n")}`)
    },
  )

  server.registerTool(
    "run_select",
    {
      title: "Довільний SELECT",
      description:
        "Запасний вихід: власний SELECT до нори, коли названі інструменти не покривають " +
        "питання. Тільки читання (роль у базі не має інших прав), лише один оператор.",
      inputSchema: {
        sql: z.string().min(6).max(4000),
        limit: z.number().int().min(1).max(500).optional(),
      },
    },
    async ({ sql, limit }) => {
      const cleaned = sql.trim().replace(/;\s*$/, "")
      if (!/^(select|with)\b/i.test(cleaned)) return text("Приймається лише SELECT або WITH.")
      if (cleaned.includes(";")) return text("Лише один оператор за раз.")
      const wrapped = `SELECT * FROM (${cleaned}) AS q LIMIT ${limit ?? 100}`
      try {
        const { rows } = await nora.query(wrapped)
        if (rows.length === 0) return text("Порожньо.")
        return text(JSON.stringify(rows, null, 1).slice(0, 40_000))
      } catch (error: any) {
        // Відмову за правами показуємо як є: це не збій, а межа доступу
        return text(`Помилка: ${error?.message ?? error}`)
      }
    },
  )

  server.registerTool(
    "whoami",
    {
      title: "Хто я і що мені видно",
      description: "Клієнт OAuth, права ролі в базі та межі даних. Діагностика підключення.",
      inputSchema: {},
    },
    async () => {
      const { rows } = await nora.query<{ n: string }>(
        `SELECT count(*)::text AS n FROM information_schema.table_privileges
          WHERE grantee = current_user AND privilege_type = 'SELECT'`,
      )
      let hidden = "телефонна база недоступна (як і задумано)"
      try {
        await nora.query("select 1 from team_contacts limit 1")
        hidden = "⚠️ team_contacts ЧИТАЄТЬСЯ — доступ ширший, ніж має бути"
      } catch {
        /* очікувано */
      }
      return text(
        `клієнт: ${auth?.clientId ?? "(токена немає)"}\n` +
          `права: ${auth?.scopes.join(", ") || "(немає)"}\n` +
          `роль у базі бачить таблиць: ${rows[0].n}\n${hidden}`,
      )
    },
  )

  return server
}
