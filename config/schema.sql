CREATE SCHEMA IF NOT EXISTS gateai;
SET search_path TO gateai, public;
CREATE TABLE IF NOT EXISTS accounts (
 id uuid PRIMARY KEY, name text NOT NULL, balance bigint NOT NULL DEFAULT 0 CHECK(balance>=0),
 daily_cap bigint NOT NULL CHECK(daily_cap>0), rpm integer NOT NULL DEFAULT 60 CHECK(rpm>0),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS api_keys (
 hash text PRIMARY KEY, account_id uuid NOT NULL REFERENCES accounts(id), revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS jobs (
 id uuid PRIMARY KEY, account_id uuid NOT NULL REFERENCES accounts(id), idempotency_key text NOT NULL,
 request_hash text NOT NULL, kind text NOT NULL, alias text NOT NULL, provider text NOT NULL,
 route jsonb NOT NULL, payload jsonb NOT NULL, status text NOT NULL DEFAULT 'queued',
 estimated_cost bigint NOT NULL, max_cost bigint NOT NULL, user_price bigint NOT NULL,
 retry_count integer NOT NULL DEFAULT 0 CHECK(retry_count BETWEEN 0 AND 1),
 provider_ref jsonb, result jsonb, error text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(account_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,updated_at);
CREATE TABLE IF NOT EXISTS transactions (
 id bigserial PRIMARY KEY, account_id uuid NOT NULL REFERENCES accounts(id), job_id uuid REFERENCES jobs(id),
 amount bigint NOT NULL, kind text NOT NULL, reference text UNIQUE, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS budgets (
 day date NOT NULL, scope text NOT NULL, reserved bigint NOT NULL DEFAULT 0 CHECK(reserved>=0), PRIMARY KEY(day,scope)
);
CREATE TABLE IF NOT EXISTS usage (
 job_id uuid PRIMARY KEY REFERENCES jobs(id), provider text NOT NULL, alias text NOT NULL,
 cost_upper_bound bigint NOT NULL, revenue bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
