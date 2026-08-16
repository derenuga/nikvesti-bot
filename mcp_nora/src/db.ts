/**
 * Дві з'єднувальні калюжі, і різниця між ними — це вся безпека цього сервісу.
 *
 * `pool` — службова база OAuth (клієнти, коди, токени). Право запису потрібне,
 * але таблиці лежать у ВЛАСНІЙ СХЕМІ `mcpauth`, а не серед таблиць нори:
 * інакше службовий мотлох OAuth змішався б із дзеркалом архіву, і будь-який
 * `\dt` у норі показував би півдесятка чужих таблиць. search_path на пулі
 * робить це прозорим — запити в oauth-provider.ts лишились без змін.
 *
 * `nora` — сама нора під роллю nora_reader: ТІЛЬКИ ЧИТАННЯ і лише
 * неперсональна частина. Всі інструменти ходять сюди й нікуди більше.
 */
import { Pool } from "pg"

import { env } from "./env.js"

export const pool = new Pool({
  connectionString: env.databaseUrl,
  max: Number(process.env.PG_POOL_MAX ?? 10),
  // Схема службових таблиць задається на рівні з'єднання, тож SQL провайдера
  // OAuth лишається таким, як у шаблоні — без жодного префікса в запитах.
  options: "-c search_path=mcpauth,public",
  // Postgres Railway доступний приватною мережею; з'єднання, що зависло на
  // конекті, має падати швидко, а не тримати запит.
  connectionTimeoutMillis: 10_000,
})

export const nora = new Pool({
  connectionString: env.noraDatabaseUrl,
  max: Number(process.env.NORA_POOL_MAX ?? 5),
  connectionTimeoutMillis: 10_000,
  // Запит, який задумався довше півхвилини, майже напевно помилка в SQL
  // інструмента, а не важкий пошук: нора вміє віддавати FTS за мілісекунди.
  statement_timeout: 30_000,
})

const SCHEMA = `
create schema if not exists mcpauth;

create table if not exists mcpauth.oauth_clients (
  client_id           text primary key,
  client_name         text,
  registered_at       timestamptz not null default now(),
  client_info         jsonb not null
);

create table if not exists mcpauth.oauth_pending_authorizations (
  id                  text primary key,
  client_id           text not null references mcpauth.oauth_clients(client_id) on delete cascade,
  redirect_uri        text not null,
  code_challenge      text not null,
  state               text,
  scopes              text[] not null default '{}',
  resource            text,
  created_at          timestamptz not null default now()
);

create table if not exists mcpauth.oauth_codes (
  code_hash           text primary key,
  client_id           text not null references mcpauth.oauth_clients(client_id) on delete cascade,
  redirect_uri        text not null,
  code_challenge      text not null,
  scopes              text[] not null default '{}',
  resource            text,
  expires_at          timestamptz not null,
  consumed_at         timestamptz
);

create table if not exists mcpauth.oauth_tokens (
  token_hash          text primary key,
  kind                text not null check (kind in ('access', 'refresh')),
  client_id           text not null references mcpauth.oauth_clients(client_id) on delete cascade,
  scopes              text[] not null default '{}',
  resource            text,
  expires_at          timestamptz not null,
  revoked_at          timestamptz
);

create index if not exists oauth_tokens_client_idx on mcpauth.oauth_tokens (client_id);

create table if not exists mcpauth.login_attempts (
  ip                  text not null,
  attempted_at        timestamptz not null default now()
);

create index if not exists login_attempts_ip_time_idx on mcpauth.login_attempts (ip, attempted_at desc);
`

export async function migrate(): Promise<void> {
  await pool.query(SCHEMA)
}

/** Перевірка на старті: роль справді читає нору і справді НЕ бачить зайвого. */
export async function checkNora(): Promise<void> {
  const { rows } = await nora.query<{ n: string }>("select count(*)::text as n from articles")
  console.log(`нора на звʼязку: ${rows[0].n} статей`)
  try {
    await nora.query("select 1 from team_contacts limit 1")
    console.error("⚠️  УВАГА: роль бачить team_contacts — доступ ширший, ніж домовлялись")
  } catch {
    console.log("межа доступу на місці: телефонна база не читається")
  }
}

/**
 * Прострочені рядки не помилка, але вони накопичуються. Коди живуть хвилину,
 * токени місяць; підмітання за розкладом тримає таблиці в межах.
 */
export async function sweepExpired(): Promise<void> {
  await pool.query(`delete from oauth_codes where expires_at < now() - interval '1 hour'`)
  await pool.query(`delete from oauth_tokens where expires_at < now() - interval '1 day'`)
  await pool.query(`delete from oauth_pending_authorizations where created_at < now() - interval '1 hour'`)
  await pool.query(`delete from login_attempts where attempted_at < now() - interval '1 day'`)
}
