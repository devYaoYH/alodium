CREATE TABLE egress_requests (
  id UUID PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  key_id TEXT NOT NULL,
  target_host TEXT NOT NULL,
  capability_id TEXT,
  request_method TEXT NOT NULL DEFAULT 'POST',
  request_path TEXT NOT NULL,
  request_headers JSONB,
  state TEXT NOT NULL CHECK (state IN ('pending', 'forwarded', 'completed', 'failed', 'denied')),
  upstream_status INTEGER,
  response_bytes BIGINT,
  duration_ms INTEGER,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX egress_requests_tenant_created_at_idx ON egress_requests (tenant_id, created_at DESC);
CREATE INDEX egress_requests_key_id_idx ON egress_requests (key_id);
CREATE INDEX egress_requests_target_host_idx ON egress_requests (target_host);
CREATE INDEX egress_requests_state_idx ON egress_requests (state);

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
