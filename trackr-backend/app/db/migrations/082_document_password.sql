-- 082_document_password.sql -- one saved password per statement source, used exactly.
--
-- Until now a locked PDF was opened by GUESSING from the stored PAN: uppercase for some
-- brokers, lowercase for Upstox, and four case variants for a CAS. That cannot open a
-- CAMS/KFintech mutual-fund CAS at all (its password is whatever the investor chose when
-- requesting it), and a guess that happens to work is not the same as knowing.
--
-- Now the investor types the password for a source once. It is tried EXACTLY as typed, and
-- it is stored only after it has actually opened one of their files -- a wrong password is
-- never saved (app/routers/ingest.py).
--
-- NOT A CREDENTIAL. A document password opens a file the investor handed us; it grants no
-- login to any broker, depository or RTA account, so docs/INGESTION.md's "no stored
-- credentials" rule is untouched. Same protection as pan_enc regardless: Fernet ciphertext,
-- key never in this database, its own table never joined into an ordinary read, and the
-- Makefile lint confines `password_enc` to app/db/document_passwords.py.

CREATE TABLE document_password (
    investor_id  uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    source       text NOT NULL CHECK (source IN
                     ('zerodha', 'groww', 'upstox', 'cdsl', 'nsdl', 'cams', 'kfintech')),
    password_enc bytea NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, source)
);

COMMENT ON TABLE document_password IS
    'Password that opens a statement PDF from one source, saved only after it opened a real '
    'file. Not a login credential. Never returned by the API; re-encrypt on key rotation '
    '(docs/KEY_ROTATION.md).';
COMMENT ON COLUMN document_password.password_enc IS
    'Fernet ciphertext of the password exactly as typed. Key never in this database.';
