"use client";

import { useRef, useState, type CSSProperties, type DragEvent } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  broadcastPortfolioRefresh,
  formatDate,
  formatMoney,
  formatQuantity,
  useAuth,
} from "@trackr/core";
import type {
  MfIssue,
  MfSchemeOut,
  MfStatementImportOut,
  MfStatementPreviewOut,
  PromoteResultOut,
} from "@trackr/core";
import { Badge, Banner, Button, CopyField, Field, LedgerGrid, PasswordInput, TabBar, type LedgerColumn } from "@trackr/ui";

/**
 * Mutual fund statement: get a CAMS + KFintech statement, then upload it.
 *
 * Two steps, the same shape as Passbook's import (one statement covers every fund house),
 * shown as tabs so either step can be reopened. Used in two places: the Import page's
 * "Mutual fund statement" card and the `/import/mutual-funds` screen (both apps).
 *
 * WHAT THE UPLOAD DOES. "Check statement" reads it and shows what is in it, writing
 * nothing. "Add to my portfolio" then sends the same file back with the digest of what was
 * shown: the server stages exactly that (or refuses if it changed) and the promotion puts
 * it in the ledger -- the same two explicit steps as every other import. Each fund is added
 * whole or not at all, with the reason on screen (app/ingest/mf_statement_rows.py).
 * The password is held in this component's memory between the two calls and dropped after
 * the second; it is never stored anywhere.
 *
 * CAMS's request form cannot be pre-filled from a website: it documents no link parameters,
 * and a page here cannot fill a form on camsonline.com (Passbook can because it is a desktop
 * app that opens CAMS inside its own window). So the settings are listed, and the two
 * dates -- the only things to type -- are worked out here, in CAMS's own format, with copy
 * buttons: the last 10 years to today, the same window Passbook requests.
 */

export const CAMS_CAS_URL =
  "https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement";

const ISSUE_LABEL: Record<MfIssue, string> = {
  NO_ISIN: "No ISIN printed",
  UNITS_DO_NOT_ADD_UP: "Units don't add up",
  HISTORY_GAP: "Bought before the statement period",
  NEEDS_EVIDENCE: "Rows need documents",
  NO_VALUATION: "No NAV printed",
  VALUE_MISMATCH: "Units × NAV ≠ value",
  AMOUNT_MISMATCH: "Units × NAV ≠ amount",
  MISSING_BALANCE: "Rows missing a balance",
  DATE_OUTSIDE_PERIOD: "Date outside statement period",
};

type Tab = "get" | "upload";

interface Row extends MfSchemeOut {
  id: string;
  [key: string]: unknown;
}

function friendly(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403)
      return "Trackr+ needs your permission before it reads any statement. Give it in setup (Permission) or on the Import page, then try again.";
    if (err.status === 413) return "This file is too large to be a CAS statement.";
    if (err.status >= 500) return "Our server had trouble reading this. Try again in a moment.";
    return err.message;
  }
  return "We couldn't reach the server. Check your connection and try again.";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "23-Oct-2016": the date format CAMS's form shows and accepts. */
function camsDate(d: Date): string {
  return `${String(d.getUTCDate()).padStart(2, "0")}-${MONTHS[d.getUTCMonth()]}-${d.getUTCFullYear()}`;
}

/** The last ten years to today (IST), as Passbook requests: 22-Oct-2026 -> from 23-Oct-2016. */
export function camsTenYearPeriod(now: Date = new Date()): { from: string; to: string } {
  const ist = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(now);
  const [y = 1970, m = 1, d = 1] = ist.split("-").map(Number);
  const to = new Date(Date.UTC(y, m - 1, d));
  const from = new Date(Date.UTC(y - 10, m - 1, d + 1));
  return { from: camsDate(from), to: camsDate(to) };
}

const listStyle: CSSProperties = {
  margin: "4px 0 0",
  paddingLeft: 20,
  display: "grid",
  gap: 6,
  color: "var(--text-secondary)",
  font: "var(--type-body)",
};

export function MfStatementImport({
  onChecked,
  onAdded,
}: {
  onChecked?: (preview: MfStatementPreviewOut) => void;
  /** After transactions reach the ledger -- e.g. to refresh an import history list. */
  onAdded?: () => void;
}) {
  const { investorId, token } = useAuth();
  const period = camsTenYearPeriod();
  const [tab, setTab] = useState<Tab>("get");
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState<MfStatementPreviewOut | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Kept only until "Add to my portfolio" re-sends the file; cleared on success or reset.
  const heldPassword = useRef("");

  function pick(next: File | null | undefined) {
    setError(null);
    if (!next) return;
    if (next.type !== "application/pdf" && !next.name.toLowerCase().endsWith(".pdf")) {
      setError("Upload the statement as a PDF: the original file from the CAMS email.");
      return;
    }
    setFile(next);
  }

  async function check() {
    if (!investorId || !file || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.set("file", file);
      // Used once by the server to open the file. Never saved, never logged.
      form.set("password", password);
      const result = await api.post<MfStatementPreviewOut>(
        `/investors/${investorId}/funds/statement-preview`,
        { formData: form, token }
      );
      setPreview(result);
      heldPassword.current = password;
      setPassword("");
      onChecked?.(result);
    } catch (err) {
      setError(friendly(err));
    } finally {
      setBusy(false);
    }
  }

  async function addToPortfolio(): Promise<PromoteResultOut> {
    if (!investorId || !file || !preview) throw new Error("Check the statement first.");
    const form = new FormData();
    form.set("file", file);
    form.set("password", heldPassword.current);
    form.set("preview_digest", preview.preview_digest);
    const staged = await api.post<MfStatementImportOut>(
      `/investors/${investorId}/funds/statement-import`,
      { formData: form, token }
    );
    heldPassword.current = "";
    const promoted = await api.post<PromoteResultOut>(`/ingest/stage/${staged.batch_id}/promote`, {
      token,
    });
    broadcastPortfolioRefresh();
    onAdded?.();
    return promoted;
  }

  function reset() {
    heldPassword.current = "";
    setPreview(null);
    setFile(null);
    setPassword("");
    setError(null);
    setTab("upload");
    if (inputRef.current) inputRef.current.value = "";
  }

  if (preview) return <PreviewResult preview={preview} onAnother={reset} onAdd={addToPortfolio} />;

  return (
    <div style={{ display: "grid", gap: 18 }}>
      <TabBar
        tabs={[
          { key: "get", label: "Step 1: Get your statement" },
          { key: "upload", label: "Step 2: Upload to Trackr+" },
        ]}
        active={tab}
        onChange={(key) => setTab(key as Tab)}
      />

      {tab === "get" ? (
        <div style={{ display: "grid", gap: 14 }}>
          <div>
            <Button
              icon="document"
              onClick={() => window.open(CAMS_CAS_URL, "_blank", "noopener,noreferrer")}
            >
              Request statement from CAMS
            </Button>
          </div>
          <p style={{ margin: 0, color: "var(--text-secondary)", font: "var(--type-body)" }}>
            This opens the CAMS website in a new tab. One statement covers funds held with CAMS
            and KFintech, so every fund house is in one file. On that page, pick:
          </p>
          <ul style={listStyle}>
            <li>
              <b>Statement type: Detailed</b> (includes transaction listing)
            </li>
            <li>
              <b>Period: Specific Period</b>, with these dates (the last 10 years):
            </li>
          </ul>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10, paddingLeft: 20 }}>
            <Field label="From date">
              <CopyField value={period.from} />
            </Field>
            <Field label="To date">
              <CopyField value={period.to} />
            </Field>
          </div>
          <ul style={listStyle}>
            <li>
              <b>Folio listing: With zero balance folios</b>, so funds you have fully sold still
              appear. If &ldquo;Without zero balance folios&rdquo; is selected, switch it.
            </li>
          </ul>
          <p style={{ margin: 0, color: "var(--text-secondary)", font: "var(--type-body)" }}>
            Enter the email linked to your mutual funds and choose a password for the PDF. CAMS
            emails it within a few minutes. Keep the password: you will need it in step 2.
          </p>
          <div>
            <Button variant="secondary" iconRight="chevron-right" onClick={() => setTab("upload")}>
              I have the PDF
            </Button>
          </div>
        </div>
      ) : (
        <div style={{ display: "grid", gap: 14 }}>
          <div
            role="button"
            tabIndex={0}
            onClick={() => inputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e: DragEvent<HTMLDivElement>) => {
              e.preventDefault();
              setDragging(false);
              pick(e.dataTransfer.files?.[0]);
            }}
            style={{
              display: "grid",
              justifyItems: "center",
              gap: 6,
              padding: "28px 16px",
              textAlign: "center",
              border: `1.5px dashed ${dragging ? "var(--accent)" : "var(--border-strong)"}`,
              borderRadius: "var(--radius-md)",
              background: dragging ? "var(--surface-hover)" : "var(--surface-sunken)",
              cursor: "pointer",
            }}
          >
            <span style={{ font: "var(--type-body)", color: "var(--text-primary)", fontWeight: 500 }}>
              {file ? file.name : "Drop your statement here or click to browse"}
            </span>
            <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
              {file ? "Click to choose a different file" : "PDF only: the original file from the CAMS email"}
            </span>
            <input
              ref={inputRef}
              type="file"
              accept="application/pdf,.pdf"
              hidden
              onChange={(e) => pick(e.target.files?.[0])}
            />
          </div>

          <Field
            label="PDF password"
            htmlFor="mf-pdf-password"
            hint="The password you chose on the CAMS website. Used once to open the file, never saved."
          >
            <PasswordInput
              id="mf-pdf-password"
              value={password}
              onChange={setPassword}
              autoComplete="off"
            />
          </Field>

          {error && <Banner tone="negative">{error}</Banner>}

          <div>
            <Button onClick={() => void check()} disabled={!file || !password || busy || !investorId}>
              {busy ? "Reading the statement…" : "Check statement"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function PreviewResult({
  preview,
  onAnother,
  onAdd,
}: {
  preview: MfStatementPreviewOut;
  onAnother: () => void;
  onAdd: () => Promise<PromoteResultOut>;
}) {
  const rows: Row[] = preview.schemes.map((s, i) => ({ ...s, id: `${s.folio_label}|${s.isin ?? s.scheme}|${i}` }));
  // A skipped fund cannot flag itself, so an incomplete read is never "all good".
  const allGood = preview.schemes_needing_attention === 0 && preview.complete;

  const columns: LedgerColumn<Row>[] = [
    {
      key: "scheme",
      label: "Fund",
      figure: false,
      width: "minmax(220px, 2.4fr)",
      render: (r) => (
        <span style={{ display: "grid", gap: 2, minWidth: 0 }}>
          <span style={{ font: "var(--type-body)", color: "var(--text-primary)" }}>{r.scheme}</span>
          <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
            {r.isin ?? "ISIN not printed"} · Folio {r.folio_label}
          </span>
        </span>
      ),
    },
    {
      key: "closing_units",
      label: "Units",
      align: "right",
      width: "minmax(100px, 1fr)",
      render: (r) => formatQuantity(r.closing_units),
    },
    {
      key: "value",
      label: "Value on statement",
      align: "right",
      width: "minmax(130px, 1fr)",
      render: (r) => formatMoney(r.value),
    },
    {
      key: "checks",
      label: "Checks",
      figure: false,
      width: "minmax(230px, 1.8fr)",
      render: (r) =>
        r.checks_passed ? (
          <Badge tone="positive">Adds up</Badge>
        ) : (
          <span style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {r.issues.map((issue) => (
              <Badge key={issue} tone="warning">
                {issue === "NEEDS_EVIDENCE" ? `${r.evidence_rows} ${ISSUE_LABEL[issue].toLowerCase()}` : ISSUE_LABEL[issue]}
              </Badge>
            ))}
          </span>
        ),
    },
    {
      key: "import",
      label: "Portfolio",
      figure: false,
      width: "minmax(110px, 0.9fr)",
      // Neutral wording, true both before and after "Add to my portfolio".
      render: (r) =>
        r.importable ? <Badge tone="positive">Included</Badge> : <Badge tone="warning">Left out</Badge>,
    },
  ];

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Banner tone={allGood ? "positive" : "warning"} title="Statement read">
        {preview.scheme_count} fund{preview.scheme_count === 1 ? "" : "s"} across {preview.folio_count} folio
        {preview.folio_count === 1 ? "" : "s"}, {preview.transaction_count} transactions
        {preview.period_from && preview.period_to ? `, ${preview.period_from} to ${preview.period_to}` : ""}.{" "}
        {allGood
          ? "Every fund's units add up."
          : !preview.complete
            ? "Part of this statement could not be read, so a fund or some rows may be missing; see below."
            : `${preview.schemes_needing_attention} fund${preview.schemes_needing_attention === 1 ? " needs" : "s need"} a look; see Checks.`}
      </Banner>

      <AddToPortfolio preview={preview} onAdd={onAdd} />

      <div style={{ overflowX: "auto" }}>
        <LedgerGrid
          gap={18}
          rows={rows}
          columns={columns}
          total={
            {
              id: "total",
              scheme: "",
              closing_units: null,
              value: preview.total_value,
              checks: null,
            } as unknown as Row
          }
        />
      </div>

      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        Values are as printed on the statement, at each fund&apos;s NAV on{" "}
        {preview.schemes[0]?.nav_date ? formatDate(preview.schemes[0].nav_date) : "the statement date"}.
        {preview.unvalued_count > 0 &&
          ` The total leaves out ${preview.unvalued_count} fund${preview.unvalued_count === 1 ? "" : "s"} with no NAV printed.`}{" "}
        &ldquo;Bought before the statement period&rdquo; means some units were bought before the
        statement starts, so their cost is not in this file: request a longer period to fill it.
      </p>

      {preview.warnings.length > 0 && (
        <Banner tone="warning" title="Part of this statement could not be read">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {preview.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Banner>
      )}

      <div>
        <Button variant="secondary" onClick={onAnother}>
          Check another statement
        </Button>
      </div>
    </div>
  );
}

function AddToPortfolio({
  preview,
  onAdd,
}: {
  preview: MfStatementPreviewOut;
  onAdd: () => Promise<PromoteResultOut>;
}) {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [result, setResult] = useState<PromoteResultOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const importable = preview.schemes.filter((s) => s.importable);
  const blocked = preview.schemes.filter((s) => !s.importable);
  const rows = importable.reduce((n, s) => n + s.ledger_rows, 0);

  async function add() {
    setState("busy");
    setError(null);
    try {
      setResult(await onAdd());
      setState("done");
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 409
          ? err.message
          : friendly(err)
      );
      setState("idle");
    }
  }

  if (state === "done" && result) {
    return (
      <Banner
        tone="positive"
        title="Added to your portfolio"
        action={<Link href="/holdings">See your holdings</Link>}
      >
        {result.inserted} transaction{result.inserted === 1 ? "" : "s"} from {importable.length} fund
        {importable.length === 1 ? "" : "s"} are now in your ledger
        {result.skipped_duplicates > 0
          ? `; ${result.skipped_duplicates} were already there and were not added twice`
          : ""}
        .
      </Banner>
    );
  }

  return (
    <div style={{ display: "grid", gap: 10 }}>
      {importable.length > 0 ? (
        <Banner tone="info" title="Add to your portfolio">
          {importable.length} fund{importable.length === 1 ? "" : "s"} ({rows} transaction
          {rows === 1 ? "" : "s"}) will be added, using the amounts printed on the statement.
          Stamp duty and STT are included with the purchase or redemption they belong to.
          {blocked.length > 0 &&
            ` ${blocked.length} fund${blocked.length === 1 ? " is" : "s are"} left out; the reason is below.`}
        </Banner>
      ) : (
        <Banner tone="warning" title="Nothing from this statement can be added yet">
          Every fund in it needs something fixed first; the reasons are below. Nothing has been saved.
        </Banner>
      )}
      {blocked.length > 0 && (
        <ul style={{ ...listStyle, margin: 0 }}>
          {blocked.map((s, i) => (
            <li key={`${s.folio_label}-${s.isin ?? s.scheme}-${i}`}>
              <b>{s.scheme}</b> (folio {s.folio_label}): {s.blocked_reason}
            </li>
          ))}
        </ul>
      )}
      {error && <Banner tone="negative">{error}</Banner>}
      {importable.length > 0 && (
        <div>
          <Button onClick={() => void add()} disabled={state === "busy"}>
            {state === "busy"
              ? "Adding…"
              : `Add ${importable.length} fund${importable.length === 1 ? "" : "s"} to my portfolio`}
          </Button>
        </div>
      )}
    </div>
  );
}
