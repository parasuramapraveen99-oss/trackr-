"use client";

import { useState } from "react";
import Link from "next/link";
import { api, ApiError, useAuth, useFetch } from "@trackr/core";
import type { AccountOut, DeclaredHoldingIn } from "@trackr/core";
import { Banner, Button, Card, Field, Input } from "@trackr/ui";
import { NoInvestorState } from "../widgets/PageState";
import type { PortfolioScreenProps } from "../chrome";
import { readEvidenceCsv } from "../reconciliation/evidence";

type Report = {
  id: string; created_at: string; account_id: string; as_of: string;
  mode: "HOLDINGS" | "HISTORY"; source: string; coverage: string;
  historical_run: string; settlement_note?: string;
  ledger_issues?: { isin: string; error: string; likely_cause: string }[];
  date_from?: string; transaction_types?: string[];
  counts?: Record<string, number>;
  matched?: number; missing_from_ledger?: number; missing_from_evidence?: number;
  rows: Record<string, string | number | null>[];
};
type Run = Pick<Report, "id" | "created_at" | "account_id" | "as_of" | "mode">;
type Statement = { id: string | null; as_of: string; positions: number; sources: string[] };
const EMPTY_ROW: DeclaredHoldingIn = { isin: "", quantity: "" };
const HISTORY_EXAMPLE = '[{"isin":"INE153T01027","txn_date":"2024-01-10","txn_type":"BUY","quantity":"100","price":"250","charges":"0","stt":"0"}]';

function validateReport(report: Report): Report {
  if (!report || !Array.isArray(report.rows) || !report.id ||
      !["HOLDINGS", "HISTORY"].includes(report.mode)) throw new Error("Invalid reconciliation response");
  const fields = report.mode === "HOLDINGS"
    ? ["computed_qty", "declared_qty", "difference", "ratio", "deficit"]
    : ["quantity", "price", "charges", "stt", "dividend_amount", "tds_amount", "cost_transfer"];
  for (const row of report.rows) {
    for (const field of fields) {
      const value = row[field];
      if (value != null && (typeof value !== "string" || !/^-?\d+(?:\.\d+)?$/.test(value))) {
        throw new Error("Invalid decimal in reconciliation response");
      }
    }
    if (row.status === "UNCOMPUTABLE" && (row.computed_qty !== null || row.difference !== null)) {
      throw new Error("Invalid missing-history result");
    }
  }
  return report;
}

export function ReconcileScreen({ chrome }: PortfolioScreenProps) {
  const { investorId, token } = useAuth();
  const { Header, Body } = chrome;
  return <><Header title="Reconcile" subtitle="Check dated quantities and transaction evidence." />
    <Body column>{investorId
      ? <ReconcileForm key={`${investorId}:${token}`} investorId={investorId} token={token} />
      : <NoInvestorState />}</Body></>;
}

function ReconcileForm({ investorId, token }: { investorId: string; token: string | null }) {
  const { asOf } = useAuth();
  const [accountId, setAccountId] = useState("");
  const [on, setOn] = useState(asOf);
  const [source, setSource] = useState("");
  const [mode, setMode] = useState<"HOLDINGS" | "HISTORY">("HOLDINGS");
  const [useSnapshot, setUseSnapshot] = useState(false);
  const [statementId, setStatementId] = useState("");
  const [complete, setComplete] = useState(false);
  const [basis, setBasis] = useState("TRADE_DATE");
  const [pending, setPending] = useState("[]");
  const [from, setFrom] = useState("");
  const [types, setTypes] = useState("BUY,SELL");
  const [evidence, setEvidence] = useState("[]");
  const [rows, setRows] = useState<DeclaredHoldingIn[]>([{ ...EMPTY_ROW }]);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const base = `/investors/${investorId}/reconcile`;
  const accounts = useFetch<AccountOut[]>(`/investors/${investorId}/accounts`, undefined, token);
  const statements = useFetch<Statement[]>(accountId ? `${base}/statements` : null,
    { account_id: accountId }, token);
  const history = useFetch<Run[]>(`${base}/runs`, undefined, token);

  async function readEvidence(file: File | undefined) {
    if (!file) return;
    setError(null); setReport(null);
    try {
      if (file.size > 2_000_000) throw new Error("Use an evidence file smaller than 2 MB.");
      const text = await file.text();
      const records = file.name.toLowerCase().endsWith(".csv") ? readEvidenceCsv(text) : JSON.parse(text);
      if (!Array.isArray(records) || !records.length) throw new Error("The file must contain transaction evidence rows.");
      setEvidence(JSON.stringify(records, null, 2));
      setSource(file.name);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not read evidence file"); }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(null); setReport(null);
    try {
      const holdings = rows.filter(r => r.isin.trim() || r.quantity.trim());
      if (holdings.some(r => !r.isin.trim() || !r.quantity.trim())) {
        throw new Error("Complete each holding row or remove it.");
      }
      const body = { account_id: accountId, as_of: on, source, mode,
        ...(mode === "HOLDINGS" ? {
          complete_statement: complete, use_snapshot: useSnapshot,
          holdings: useSnapshot ? [] : holdings, basis,
          statement_id: useSnapshot && statementId ? statementId : undefined,
          pending_settlements: basis === "SETTLED" ? JSON.parse(pending) : [],
        } : {
          date_from: from, transaction_types: types.split(",").map(t => t.trim()).filter(Boolean),
          evidence: JSON.parse(evidence),
        }) };
      const result = await api.post<Report>(`${base}/runs`, { json: body, token });
      setReport(validateReport(result)); history.refetch();
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : "Reconciliation failed");
    } finally { setBusy(false); }
  }

  async function openRun(id: string) {
    setBusy(true); setError(null); setReport(null);
    try { setReport(validateReport(await api.get<Report>(`${base}/runs/${id}`, { token }))); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not load run"); }
    finally { setBusy(false); }
  }

  function download() {
    if (!report) return;
    // JSON preserves exact decimals and every coverage/error field; no spreadsheet
    // formula interpretation and no fetched exchange price data in this export.
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)],
      { type: "application/json" }));
    const link = document.createElement("a"); link.href = url;
    link.download = `reconciliation-${report.id}.json`; link.click();
    URL.revokeObjectURL(url);
  }

  return <div style={{ display: "grid", gap: 24 }}>
    <Card title="Comparison scope">
      <Banner tone="info" title="A quantity match has a specific meaning">
        Quantities can match while acquisition costs or a fully sold investment are missing.
        Use transaction history to compare the source records, including closed positions.
      </Banner>
      <p><Link href="/import">Import a CAS or broker document</Link></p>
      <form onSubmit={submit} style={{ display: "grid", gap: 16 }}>
        <fieldset disabled={busy} style={{ border: 0, padding: 0, display: "grid", gap: 16 }}>
          <label>Account <select required value={accountId} onChange={e => {
            setAccountId(e.target.value); setReport(null); setUseSnapshot(false); setComplete(false); setStatementId("");
          }}><option value="">Choose account</option>{accounts.data?.map(a =>
            <option key={a.id} value={a.id}>{a.label || a.broker} · {a.id.slice(0, 8)}</option>)}</select></label>
          {accounts.error && <p role="alert">{accounts.error}</p>}
          <label>Check <select value={mode} onChange={e => {
            setMode(e.target.value as typeof mode); setReport(null);
          }}><option value="HOLDINGS">Holdings on a date</option>
            <option value="HISTORY">Transaction history (including sold positions)</option></select></label>
          <Field htmlFor="recon-date" label="Statement / period end date"><Input id="recon-date" type="date" value={on} onChange={setOn} /></Field>
          <Field htmlFor="recon-source" label="Source document reference" hint="Required. Identify the document used for this comparison.">
            <Input id="recon-source" value={source} onChange={setSource} placeholder="e.g. broker statement dated 30 September" />
          </Field>
          {mode === "HOLDINGS" ? <>
            <label><input type="checkbox" checked={useSnapshot} onChange={e => setUseSnapshot(e.target.checked)} /> Use imported holdings observations</label>
            {useSnapshot ? <label>Imported date <select value={statementId || on} onChange={e => {
              const item = statements.data?.find(s => (s.id || s.as_of) === e.target.value);
              setStatementId(item?.id || ""); setOn(item?.as_of || ""); setComplete(false);
            }}><option value="">Choose statement date</option>{statements.data?.map(s =>
              <option key={s.id || s.as_of} value={s.id || s.as_of}>{s.as_of} · {s.positions} observations · {s.sources.join(", ")}</option>)}</select>
              {statements.error && <span role="alert">{statements.error}</span>}
              <p>Imported observations have unverified completeness. Confirm below only after checking the full account statement.</p>
            </label> : rows.map((r, i) => <div key={i} style={{ display: "flex", gap: 8 }}>
              <label>ISIN <Input value={r.isin} placeholder="ISIN" id={`recon-isin-${i}`} onChange={value => setRows(old => old.map((row, n) => n === i ? { ...row, isin: value.toUpperCase() } : row))} /></label>
              <label>Quantity <Input value={r.quantity} placeholder="Quantity" id={`recon-quantity-${i}`} onChange={value => setRows(old => old.map((row, n) => n === i ? { ...row, quantity: value } : row))} /></label>
              <Button variant="ghost" onClick={() => setRows(old => old.filter((_, n) => n !== i))}>Remove</Button>
            </div>)}
            {!useSnapshot && <Button variant="ghost" onClick={() => setRows(old => [...old, { ...EMPTY_ROW }])}>Add holding</Button>}
            <label><input type="checkbox" checked={complete} onChange={e => setComplete(e.target.checked)} /> This is the complete statement for this account and date (including an explicitly empty account).</label>
            <label>Statement balance basis <select value={basis} onChange={e => setBasis(e.target.value)}>
              <option value="TRADE_DATE">Trade date</option><option value="SETTLED">Settled holdings</option>
            </select></label>
            {basis === "SETTLED" && <label>Optional unsettled trade evidence (JSON)
              <textarea rows={4} value={pending} onChange={e => setPending(e.target.value)} style={{ width: "100%" }} />
              <small>Each entry needs transaction_id, settlement_date and evidence_ref. No timing adjustment is guessed.</small>
            </label>}
          </> : <>
            <Field htmlFor="recon-from" label="Period start date"><Input id="recon-from" type="date" value={from} onChange={setFrom} /></Field>
            <Field htmlFor="recon-types" label="Covered transaction types" hint="Comma-separated types covered by this document, e.g. BUY,SELL,IPO_ALLOT,DEMERGER_IN">
              <Input id="recon-types" value={types} onChange={setTypes} />
            </Field>
            <Field label="Upload transaction evidence" hint="CSV in the documented format or a saved JSON evidence file. This compares records and does not import trades.">
              <input type="file" accept=".csv,.json" onChange={(e) => void readEvidence(e.target.files?.[0])} />
            </Field>
            <p style={{ color: "var(--text-tertiary)", fontSize: 12 }}>CSV columns: isin, txn_date, txn_type, quantity, price, charges, stt. Use dates as YYYY-MM-DD and the actual evidenced amounts. Broker import files belong on the Import screen.</p>
            <details><summary>Review or enter evidence manually</summary><label>Source transactions (JSON, decimal values must be strings)
              <textarea rows={8} value={evidence} onChange={e => setEvidence(e.target.value)} style={{ width: "100%" }} />
              <small>Required per row: ISIN, date, type, quantity, price, charges and STT. Enter actual evidenced charges; do not assume zero when unknown.</small>
              <details><summary>Example format</summary><pre style={{ whiteSpace: "pre-wrap" }}>{HISTORY_EXAMPLE}</pre></details>
            </label></details>
          </>}
          <Button type="submit" disabled={!accountId || !source.trim() || !on || busy}>{busy ? "Comparing…" : "Compare and save run"}</Button>
        </fieldset>
      </form>
    </Card>
    {error && <p role="alert">{error}</p>}
    {report && <Card title={`Saved ${report.mode === "HOLDINGS" ? "holdings" : "history"} comparison · ${report.as_of}`}>
      <p>Account {report.account_id} · Source: {report.source} · Run: {report.created_at}</p>
      <Banner tone="info" title="Coverage">{report.coverage} {report.historical_run}</Banner>
      {report.ledger_issues?.map((issue, index) => <p role="alert" key={`${issue.isin}:${index}`}>Cannot reconcile—missing history: {issue.isin}. {issue.error || issue.likely_cause}</p>)}
      {report.date_from && <p>Period: {report.date_from} to {report.as_of} · Types: {report.transaction_types?.join(", ")}</p>}
      {report.settlement_note && <p>{report.settlement_note}</p>}
      <p>{report.counts ? Object.entries(report.counts).map(([k, n]) => `${k}: ${n}`).join(" · ")
        : `Matched records: ${report.matched} · Missing from ledger: ${report.missing_from_ledger} · Missing from evidence: ${report.missing_from_evidence}`}</p>
      {report.rows.length === 0 && <p>No rows compared. This does not verify historical activity.</p>}
      <div style={{ overflowX: "auto" }}><table><thead><tr>
        {(report.mode === "HOLDINGS" ? ["Status", "ISIN", "Trackr+", "Statement", "Difference", "Details"] : ["Status", "ISIN", "Date", "Type", "Quantity", "Price", "Charges", "STT", "Count"]).map(h => <th key={h} style={{ padding: 8, textAlign: "left" }}>{h}</th>)}
      </tr></thead><tbody>{report.rows.map((r, i) => <tr key={i}>
        {(report.mode === "HOLDINGS" ? [r.status, r.isin, r.computed_qty, r.declared_qty, r.difference,
          [r.likely_cause, r.error_date, r.deficit ? `Shortfall: ${r.deficit}` : ""].filter(Boolean).join(" · ")]
          : [r.status, r.isin, r.txn_date, r.txn_type, r.quantity, r.price, r.charges, r.stt, r.count])
          .map((value, n) => <td key={n} style={{ padding: 8 }}>{value === null || value === undefined ? "Unknown / not checked" : String(value)}</td>)}
      </tr>)}</tbody></table></div>
      <Button variant="ghost" onClick={download}>Download full report (JSON)</Button>
    </Card>}
    <Card title="Previous runs" subtitle="Saved observations, not a live certification">
      {history.error && <p role="alert">{history.error}</p>}
      {history.data?.filter(r => !accountId || r.account_id === accountId).map(r => <p key={r.id}>
        <button type="button" disabled={busy} onClick={() => openRun(r.id)}>{r.as_of} · {r.mode} · {r.created_at}</button>
      </p>)}
    </Card>
  </div>;
}
