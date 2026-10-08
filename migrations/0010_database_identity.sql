-- 0010: what kind of EYE database this is: the synthetic demo or a private
-- installation (review finding: demo startup and demo writes trusted the
-- configured mode and a loopback connection only).
--
-- Review notes (see migrations/README.md for process and restore):
-- * Forward only; 0001 to 0009 are unchanged. New objects only: one table
--   and its append-only trigger. No existing row is read or rewritten.
-- * At most one row. It is claimed once and never changed: by `db-migrate`
--   or `db-prepare-demo` in demo mode ('synthetic-demo'), or by `db-migrate`
--   in production mode ('private'). A database claimed for one kind refuses
--   the other for good; there is no command that relabels it.
-- * Only those two commands claim a database. Serving and every other command
--   need the claim of their mode first. The demo also refuses a database
--   holding a capture batch of a non-synthetic source, and production one
--   holding a synthetic source, before they serve or write anything.
-- * Expected lock impact: none while migrating (the table is created empty).

CREATE TABLE eye.database_identity (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    kind       text NOT NULL CHECK (kind IN ('synthetic-demo', 'private')),
    claimed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER database_identity_append_only BEFORE UPDATE OR DELETE ON eye.database_identity
    FOR EACH ROW EXECUTE FUNCTION eye.refuse_change();
