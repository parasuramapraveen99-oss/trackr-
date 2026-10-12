-- 004_grievance.sql -- grievance intake with the DPDP 90-day clock.
--
-- docs/COMPLIANCE.md: "90-day ceiling on grievance redressal" and "a grievance intake
-- route with a clock". A ceiling, not a target: 89 days is compliant and terrible.

CREATE TYPE grievance_status AS ENUM ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'ESCALATED');

CREATE TABLE grievance (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id     uuid REFERENCES investor(id) ON DELETE SET NULL,
    reference       text NOT NULL UNIQUE,
    channel         text NOT NULL,
    subject         text NOT NULL,
    detail          text,
    status          grievance_status NOT NULL DEFAULT 'OPEN',
    raised_at       timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    resolved_at     timestamptz,
    resolution      text,
    assigned_to     uuid,

    -- THE CLOCK. Maintained by a trigger, not by whoever writes the row.
    -- (A generated column cannot express this: timestamptz + interval depends on the
    -- session timezone, so Postgres rejects it as non-immutable.)
    due_at timestamptz,

    -- A grievance cannot be closed without saying how. "RESOLVED" with an empty
    -- resolution is how a redressal record becomes worthless in the dispute it exists for.
    CONSTRAINT resolved_needs_a_resolution CHECK (
        status <> 'RESOLVED'
        OR (resolved_at IS NOT NULL AND resolution IS NOT NULL AND length(trim(resolution)) > 0)
    ),
    -- Resolution cannot precede the complaint. Backdating a closure to land inside the
    -- window would fake compliance in exactly the record meant to evidence it.
    CONSTRAINT resolution_follows_the_complaint CHECK (
        resolved_at IS NULL OR resolved_at >= raised_at
    )
);

COMMENT ON TABLE grievance IS
    'DPDP grievance redressal, 90-day ceiling. investor_id is ON DELETE SET NULL rather '
    'than CASCADE: if an investor exercises erasure, the grievance record survives '
    'without them attached, because the fact that a complaint was made and how it was '
    'handled is a record of OUR conduct, not only of theirs.';
COMMENT ON COLUMN grievance.due_at IS
    'Always raised_at + 90 days, recomputed by trigger on every write. Any value supplied '
    'by a caller is discarded, so the deadline cannot be quietly extended.';
COMMENT ON COLUMN grievance.reference IS
    'Externally quotable. Give this to the complainant, never the internal uuid.';

-- A deadline an application sets is a deadline an application can set wrongly. This
-- overwrites whatever was supplied, on INSERT and on UPDATE alike, so there is no path -
-- application, migration or manual psql - that moves a statutory ceiling.
CREATE FUNCTION grievance_set_due_at() RETURNS trigger AS $$
BEGIN
    NEW.due_at := NEW.raised_at + interval '90 days';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER grievance_due_at_is_derived
    BEFORE INSERT OR UPDATE ON grievance
    FOR EACH ROW EXECUTE FUNCTION grievance_set_due_at();

CREATE INDEX grievance_open_idx ON grievance (due_at)
    WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'ESCALATED');
CREATE INDEX grievance_investor_idx ON grievance (investor_id);
