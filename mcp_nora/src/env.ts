/**
 * Налаштування, читаються раз на старті.
 *
 * Сервер відмовляється стартувати на кривому значенні, а не падає потім
 * усередині запиту: наполовину налаштований OAuth гірший за явно вимкнений.
 *
 * ДВІ БАЗИ, і це головне в цьому файлі:
 *   DATABASE_URL      — службова, з правом ЗАПИСУ: у ній живуть клієнти OAuth,
 *                       коди й токени (схема mcpauth, див. db.ts);
 *   NORA_DATABASE_URL — сама нора під роллю nora_reader, ТІЛЬКИ ЧИТАННЯ і
 *                       лише неперсональна частина (scripts/sql/nora_reader.sql).
 * Розділення не косметичне: навіть якщо в інструменті колись зʼявиться помилка,
 * запитом із неї не можна ні написати в нору, ні побачити телефонну базу,
 * лікарняні чи особистий блокнот — прав немає в самої ролі.
 */

function required(name: string): string {
  const value = process.env[name]
  if (!value) {
    console.error(`${name} не задано. Сервер без цього не стартує.`)
    process.exit(1)
  }
  return value
}

const publicUrl =
  process.env.PUBLIC_URL ||
  (process.env.RAILWAY_PUBLIC_DOMAIN ? `https://${process.env.RAILWAY_PUBLIC_DOMAIN}` : "")

if (!publicUrl) {
  console.error("PUBLIC_URL не задано і публічного домену немає. OAuth мусить знати власну адресу.")
  process.exit(1)
}

// Ідентифікатор видавця йде в підписані метадані й у перевірку аудиторії
// кожного токена, тож це має бути ТОЧНО той origin, на який приходять клієнти —
// без слеша в кінці й без шляху.
const issuer = new URL(publicUrl).origin

const adminPassword = required("MCP_ADMIN_PASSWORD")

if (adminPassword.length < 12) {
  console.error("MCP_ADMIN_PASSWORD коротший за 12 символів. Візьми довший.")
  process.exit(1)
}

export const env = {
  port: Number(process.env.PORT ?? 8080),
  databaseUrl: required("DATABASE_URL"),
  noraDatabaseUrl: required("NORA_DATABASE_URL"),
  issuer,
  adminPassword,
  serverName: process.env.MCP_SERVER_NAME || "Нора МикВісті",
  // Токен доступу живе годину: за ним стоїть refresh, тож коротке життя
  // обмежує шкоду від витоку і не змушує клієнта перепідключатись.
  accessTokenTtlSeconds: Number(process.env.ACCESS_TOKEN_TTL_SECONDS ?? 3600),
  refreshTokenTtlSeconds: Number(process.env.REFRESH_TOKEN_TTL_SECONDS ?? 60 * 60 * 24 * 30),
  authorizationCodeTtlSeconds: 60,
  scopes: ["mcp:tools"],
}

export type Env = typeof env
