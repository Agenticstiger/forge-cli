-- Seed schema + sample rows for the Fluid MCP output-port Postgres
-- e2e demo. Loaded by the postgres:16-alpine container at startup
-- via the standard /docker-entrypoint-initdb.d/ hook.
--
-- The contact_email and medical_note columns carry deliberately
-- look-like-PII / look-like-PHI values so the row-level redaction
-- pass (sensitivity: pii / phi in the contract schema) has
-- something obvious to redact in the e2e output.

CREATE SCHEMA IF NOT EXISTS telco_demo;

CREATE TABLE IF NOT EXISTS telco_demo.customer_segments (
  customer_id        TEXT      PRIMARY KEY,
  segment            TEXT      NOT NULL,
  signup_date        DATE      NOT NULL,
  lifetime_value_usd NUMERIC   NOT NULL,
  contact_email      TEXT      NOT NULL,
  medical_note       TEXT      NOT NULL
);

INSERT INTO telco_demo.customer_segments
  (customer_id, segment,    signup_date,  lifetime_value_usd, contact_email,           medical_note)
VALUES
  ('TELCO-0001','enterprise','2024-01-15',12500.00,           'alice@enterprise.com',   'allergic to penicillin'),
  ('TELCO-0002','smb',       '2024-02-10',4500.00,            'bob@smb-startup.io',     'managed type-2 diabetes'),
  ('TELCO-0003','consumer',  '2024-03-05',890.50,             'charlie@example.com',    'no notes'),
  ('TELCO-0004','enterprise','2024-04-22',31200.75,           'dora@bigcorp.example',   'on beta-blockers'),
  ('TELCO-0005','smb',       '2024-05-30',7850.40,            'eve@smb.example',        'no notes'),
  ('TELCO-0006','consumer',  '2024-06-14',412.10,             'frank@example.com',      'asthmatic'),
  ('TELCO-0007','enterprise','2024-07-09',55600.00,           'grace@megacorp.example', 'no notes'),
  ('TELCO-0008','consumer',  '2024-08-20',1225.65,            'henry@example.com',      'no notes')
ON CONFLICT (customer_id) DO NOTHING;

-- Read-only role the gateway connects with. Mirrors what an
-- enterprise operator should do in production: never give the
-- gateway credentials with write access.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'forge_mcp_reader') THEN
    CREATE ROLE forge_mcp_reader LOGIN PASSWORD 'forge_mcp_reader_pwd';
  END IF;
END$$;
GRANT USAGE ON SCHEMA telco_demo TO forge_mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA telco_demo TO forge_mcp_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA telco_demo GRANT SELECT ON TABLES TO forge_mcp_reader;
