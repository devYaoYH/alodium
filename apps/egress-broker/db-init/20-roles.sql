\getenv writer_password EGRESS_AUDIT_DB_WRITER_PASSWORD
\getenv reader_password EGRESS_AUDIT_DB_READER_PASSWORD

CREATE ROLE egress_audit_writer LOGIN PASSWORD :'writer_password';
CREATE ROLE egress_audit_reader LOGIN PASSWORD :'reader_password';
GRANT USAGE ON SCHEMA public TO egress_audit_writer, egress_audit_reader;
GRANT SELECT, INSERT, UPDATE ON egress_requests TO egress_audit_writer;
GRANT SELECT ON egress_requests TO egress_audit_reader;
