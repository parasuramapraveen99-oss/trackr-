-- 008_auth.sql -- login to THIS application, not to a broker.
--
-- Distinct from everything else in the schema: every other table describes an
-- investor's holdings or the compliance trail around them. This one describes who is
-- allowed to operate the application -- the adviser, not the client's broker account.
-- app_user.id is what `actor_uid` has meant informally everywhere else in this schema
-- (audit_log.actor_uid, import_batch.promoted_by): the identity a mutation is attributed
-- to. This migration is what finally gives that id a table to reference.
--
-- NOT a broker credential table. No broker password, API secret or OTP lives here or
-- anywhere else in this system -- see CLAUDE.md rule 6 and `make lint`'s credential-name
-- check. app_session stores a HASH of the session token, never the token itself, for the
-- same reason a PAN is never stored unencrypted: a stolen database must not also be a
-- stolen set of live sessions.

CREATE TABLE app_user (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    display_name  text,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE app_user IS
    'Login to this application. Not an investor and not a broker account -- an adviser '
    'operating the platform on behalf of one or more investor rows.';
COMMENT ON COLUMN app_user.password_hash IS
    'bcrypt. Never the plaintext password, never reversible.';

CREATE TABLE app_session (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    session_token_hash text NOT NULL UNIQUE,
    issued_at          timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL,
    revoked_at         timestamptz
);
COMMENT ON TABLE app_session IS
    'One row per issued JWT, so a session can be listed or revoked. The access token '
    'itself is stateless and self-verifying; this table is what makes revocation and '
    '"log out everywhere" possible for something that would otherwise be valid until it '
    'simply expires.';
COMMENT ON COLUMN app_session.session_token_hash IS
    'SHA-256 of the issued token, never the token itself -- named for what it holds (a '
    'hash of a session credential) rather than after either OAuth2 token vocabulary '
    'term, so it reads unambiguously to both a human reviewer and `make lint`''s '
    'credential-name check.';

CREATE INDEX app_session_user_idx ON app_session (user_id);
CREATE INDEX app_session_active_idx ON app_session (expires_at) WHERE revoked_at IS NULL;
