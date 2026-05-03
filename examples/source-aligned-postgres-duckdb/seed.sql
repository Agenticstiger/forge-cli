-- Seed for the source-aligned-postgres-duckdb example. Loaded by
-- docker-compose on first container start.

CREATE TABLE IF NOT EXISTS public.orders (
    id          BIGINT PRIMARY KEY,
    customer    TEXT NOT NULL,
    amount      NUMERIC(12, 2) NOT NULL,
    placed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO public.orders (id, customer, amount, placed_at) VALUES
    (1, 'Alice',   100.50, '2026-04-01T10:00:00Z'),
    (2, 'Bob',     250.00, '2026-04-02T14:00:00Z'),
    (3, 'Carol',    42.00, '2026-04-03T09:30:00Z'),
    (4, 'Diane',   900.00, '2026-04-04T16:15:00Z'),
    (5, 'Eve',      55.55, '2026-04-05T11:45:00Z')
ON CONFLICT (id) DO NOTHING;
