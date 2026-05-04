-- Seed data for the engine-matrix verification harness.
CREATE TABLE IF NOT EXISTS public.orders (
    id          BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    total_cents BIGINT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);

INSERT INTO public.orders (id, customer_id, total_cents, created_at) VALUES
    (1, 100, 1999, '2026-04-01T10:00:00Z'),
    (2, 100, 2599, '2026-04-02T11:00:00Z'),
    (3, 101, 4999, '2026-04-03T12:00:00Z'),
    (4, 102,  799, '2026-04-04T09:00:00Z'),
    (5, 103, 3299, '2026-04-05T08:30:00Z')
ON CONFLICT (id) DO NOTHING;

-- Replication identity for Debezium CDC.
ALTER TABLE public.orders REPLICA IDENTITY FULL;
