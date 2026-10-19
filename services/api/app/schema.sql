-- Idempotent schema (safe to run on every start). For real production use a
-- versioned migration tool (golang-migrate, Atlas, goose) instead.

CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text UNIQUE NOT NULL,
    password_hash text NOT NULL,                       -- bcrypt, never plaintext
    role          text NOT NULL CHECK (role IN ('viewer','rep','manager','admin')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    id               bigserial PRIMARY KEY,
    name             text UNIQUE NOT NULL,
    sector           text,
    year_established int,
    revenue_musd     numeric(14,2),                    -- revenue in millions USD (sensitive: needs revenue:read)
    employees        int,
    office_location  text,
    subsidiary_of    text
);

CREATE TABLE IF NOT EXISTS products (
    name        text PRIMARY KEY,
    series      text,
    sales_price numeric(12,2)
);

CREATE TABLE IF NOT EXISTS sales_agents (
    name            text PRIMARY KEY,
    manager         text,
    regional_office text
);

CREATE TABLE IF NOT EXISTS opportunities (
    id          text PRIMARY KEY,
    agent       text NOT NULL REFERENCES sales_agents(name),
    product     text NOT NULL REFERENCES products(name),
    account_id  bigint REFERENCES accounts(id),        -- NULL while still "Prospecting"
    stage       text NOT NULL CHECK (stage IN ('Prospecting','Engaging','Won','Lost')),
    engage_date date,
    close_date  date,
    close_value numeric(12,2),                         -- sensitive: needs deal_value:read
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_opp_stage ON opportunities(stage);
CREATE INDEX IF NOT EXISTS idx_opp_agent ON opportunities(agent);

-- Append-only audit trail: who did what, when, from where.
CREATE TABLE IF NOT EXISTS audit_log (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    request_id text,
    user_id    text,
    email      text,
    role       text,
    method     text NOT NULL,
    path       text NOT NULL,
    status     int  NOT NULL,
    ip         text,
    detail     jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

-- Governance: even the application DB user cannot edit or delete audit rows.
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_immutable ON audit_log;
CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
