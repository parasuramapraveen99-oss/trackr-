"use client";

import Link from "next/link";
import { useState, type CSSProperties } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { EraseResultOut, PrivacyExportOut } from "@/lib/api-types";
import { NoInvestorState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { PageBody } from "../components/Shell";
import { Banner, Button, Card, Checkbox, Input } from "../components/ds";

// Must be typed exactly, not just any non-empty text -- this is the type-to-confirm
// gate in front of an irreversible delete (app/db/privacy.py erase_investor).
const ERASE_CONFIRM_PHRASE = "DELETE MY PORTFOLIO";

function downloadJson(payload: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const bodyText: CSSProperties = { margin: 0, font: "var(--type-body)", color: "var(--text-secondary)" };
const noteText: CSSProperties = { margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" };
const mono: CSSProperties = { fontFamily: "var(--font-mono)" };

/**
 * Privacy: export everything, or delete everything.
 *
 * IT USED TO LOOK LIKE A DIFFERENT PRODUCT. This was the last screen written against the
 * retired Tailwind names (`glass-card`, `navy-*`, `brass-*`, `rounded-lg`) and against
 * PageState's `Card` rather than the design system's, and it drew its own `<h1>` in the body
 * instead of using `PageHeader`. globals.css repoints those colours, so it resolved to
 * roughly the right palette and the wrong shapes: 12px radii and glass panels, on a screen
 * you reach from Settings, which is square panels and hairline rules. Moving between the two
 * read as leaving the app. It is now the same chrome as every other screen -- a page header,
 * one centred `--page-column`, design-system cards, buttons and fields.
 *
 * WHAT DID NOT CHANGE is any guard. The type-to-confirm phrase, the separately audited
 * export of PAN and demat behind its own acknowledgement, the legal-hold refusal shown with
 * its named reasons, and the erased-account screen that still renders after `investorId` is
 * gone. Those are the screen. The rest was upholstery.
 */
export default function PrivacyPage() {
  const { investorId, token, setInvestorId } = useAuth();

  const [exporting, setExporting] = useState(false);
  const [exportingIdentifiers, setExportingIdentifiers] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportResult, setExportResult] = useState<PrivacyExportOut | null>(null);
  const [exportedIdentifiers, setExportedIdentifiers] = useState(false);
  const [showJson, setShowJson] = useState(false);

  const [identifiersPanelOpen, setIdentifiersPanelOpen] = useState(false);
  const [identifiersAck, setIdentifiersAck] = useState(false);

  const [erasePanelOpen, setErasePanelOpen] = useState(false);
  const [eraseConfirmText, setEraseConfirmText] = useState("");
  const [erasing, setErasing] = useState(false);
  const [eraseError, setEraseError] = useState<string | null>(null);
  const [legalHoldReasons, setLegalHoldReasons] = useState<string[] | null>(null);
  const [eraseResult, setEraseResult] = useState<EraseResultOut | null>(null);

  // Erasure clears investorId (the record no longer exists), so the success screen
  // below must render even though the "no investor" guard would otherwise fire.
  if (!investorId && !eraseResult) {
    return (
      <>
        <PageHeader
          title="Privacy & your data"
          subtitle="Download or delete a portfolio's data."
        />
        <PageBody column>
          <NoInvestorState />
        </PageBody>
      </>
    );
  }

  async function runExport(includeIdentifiers: boolean) {
    const setBusy = includeIdentifiers ? setExportingIdentifiers : setExporting;
    setBusy(true);
    setExportError(null);
    try {
      const res = await api.get<PrivacyExportOut>("/privacy/export", {
        token,
        query: { investor_id: investorId, include_identifiers: includeIdentifiers },
      });
      setExportResult(res);
      setExportedIdentifiers(includeIdentifiers);
      setShowJson(false);
      setIdentifiersPanelOpen(false);
      setIdentifiersAck(false);
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }

  async function runErase() {
    if (eraseConfirmText !== ERASE_CONFIRM_PHRASE) return;
    setErasing(true);
    setEraseError(null);
    setLegalHoldReasons(null);
    try {
      const res = await api.post<EraseResultOut>("/privacy/erase", {
        token,
        query: { investor_id: investorId },
      });
      setEraseResult(res);
      setInvestorId("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.reasons) {
        // Legal-hold refusal -- named reasons, not a generic failure toast.
        setLegalHoldReasons(err.reasons);
      } else {
        setEraseError(err instanceof ApiError ? err.message : "Erasure failed");
      }
    } finally {
      setErasing(false);
    }
  }

  if (eraseResult) {
    return (
      <>
        <PageHeader title="Privacy & your data" subtitle="This portfolio has been deleted." />
        <PageBody column>
          <Card title="Portfolio erased">
            <div style={{ display: "grid", gap: 10 }}>
              <p style={bodyText}>
                This investor&apos;s data has been permanently deleted: {eraseResult.investors} investor record,{" "}
                {eraseResult.transactions} transaction{eraseResult.transactions === 1 ? "" : "s"} and{" "}
                {eraseResult.documents} stored document{eraseResult.documents === 1 ? "" : "s"} removed.
              </p>
              <p style={noteText}>
                We keep a note that a deletion happened, as the law requires. None of the data is kept.
              </p>
              <p style={noteText}>
                <Link href="/settings#portfolios">Open Settings</Link> to pick another portfolio.
              </p>
            </div>
          </Card>
        </PageBody>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Privacy & your data"
        subtitle="Download or delete this portfolio's data."
      />

      <PageBody column>
        <div style={{ display: "grid", gap: 20 }}>
          <Card
            title="Export your data"
            subtitle={
              <>
                Portfolio ID <span style={mono}>{investorId}</span>
              </>
            }
          >
            <div style={{ display: "grid", gap: 14 }}>
              <p style={bodyText}>
                Download a full copy of this portfolio: accounts, transactions and documents.
              </p>

              {exportError && <Banner tone="negative">{exportError}</Banner>}

              <div>
                <Button onClick={() => runExport(false)} disabled={exporting}>
                  {exporting ? "Exporting…" : "Export my data"}
                </Button>
              </div>

              {exportResult && (
                <div
                  style={{
                    display: "grid",
                    gap: 12,
                    border: "1px solid var(--border)",
                    background: "var(--surface-sunken)",
                    padding: 14,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 10,
                    }}
                  >
                    <p
                      style={{
                        margin: 0,
                        font: "var(--type-body-sm)",
                        fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
                        color: "var(--text-primary)",
                      }}
                    >
                      Export ready{exportedIdentifiers ? ", includes PAN and demat" : ""}
                    </p>
                    <div style={{ display: "flex", gap: 8 }}>
                      <Button variant="ghost" size="sm" onClick={() => setShowJson((v) => !v)}>
                        {showJson ? "Hide JSON" : "View JSON"}
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => downloadJson(exportResult, `trackr-export-${investorId}.json`)}
                      >
                        Download JSON
                      </Button>
                    </div>
                  </div>

                  <dl
                    style={{
                      margin: 0,
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))",
                      gap: 10,
                    }}
                  >
                    <Count label="Accounts" value={exportResult.accounts.length} />
                    <Count label="Transactions" value={exportResult.transactions.length} />
                    <Count label="Documents" value={exportResult.documents.length} />
                    <Count label="Consents" value={exportResult.consents.length} />
                  </dl>

                  {showJson && (
                    <pre
                      style={{
                        margin: 0,
                        maxHeight: 340,
                        overflow: "auto",
                        background: "var(--ink-900)",
                        color: "var(--text-inverse)",
                        padding: 12,
                        fontFamily: "var(--font-mono)",
                        fontSize: 11,
                        lineHeight: 1.6,
                      }}
                    >
                      {JSON.stringify(exportResult, null, 2)}
                    </pre>
                  )}
                </div>
              )}

              <div style={{ borderTop: "1px dashed var(--border)", paddingTop: 14 }}>
                {!identifiersPanelOpen ? (
                  <Button variant="ghost" size="sm" onClick={() => setIdentifiersPanelOpen(true)}>
                    Include PAN and demat number in the export
                  </Button>
                ) : (
                  <div
                    style={{
                      display: "grid",
                      gap: 12,
                      border: "1px solid var(--border)",
                      borderLeft: "2px solid var(--warning)",
                      background: "var(--surface)",
                      padding: 14,
                    }}
                  >
                    <p style={{ ...bodyText, font: "var(--type-body-sm)" }}>
                      This adds PAN and demat number to the file. Each use is logged. Only do this if you need them,
                      for example when moving to another adviser.
                    </p>
                    <Checkbox
                      id="privacy-identifiers-ack"
                      checked={identifiersAck}
                      onChange={setIdentifiersAck}
                      label={
                        <span style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
                          I understand PAN and demat number will be in the file.
                        </span>
                      }
                    />
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      <Button
                        size="sm"
                        disabled={!identifiersAck || exportingIdentifiers}
                        onClick={() => runExport(true)}
                      >
                        {exportingIdentifiers ? "Exporting…" : "Export with PAN and demat"}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setIdentifiersPanelOpen(false);
                          setIdentifiersAck(false);
                        }}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </Card>

          <Card title="Delete this portfolio">
            <div style={{ display: "grid", gap: 14 }}>
              <p style={bodyText}>
                Deletes all accounts, transactions and documents for this portfolio. This cannot be undone.
              </p>

              {eraseError && <Banner tone="negative">{eraseError}</Banner>}

              {legalHoldReasons && (
                <Banner tone="negative" title="Cannot delete right now">
                  This portfolio is on legal hold, so its records must be kept for now.
                  <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                    {legalHoldReasons.map((reason, i) => (
                      <li key={i}>{reason}</li>
                    ))}
                  </ul>
                  <p style={{ margin: "8px 0 0" }}>
                    Contact support if you think this is wrong.
                  </p>
                </Banner>
              )}

              {!erasePanelOpen ? (
                <div>
                  <Button variant="danger" icon="trash" onClick={() => setErasePanelOpen(true)}>
                    Delete this portfolio
                  </Button>
                </div>
              ) : (
                <div
                  style={{
                    display: "grid",
                    gap: 12,
                    border: "1px solid var(--border)",
                    borderLeft: "2px solid var(--negative)",
                    background: "var(--surface)",
                    padding: 14,
                  }}
                >
                  <p style={{ ...bodyText, font: "var(--type-body-sm)" }}>
                    Type <span style={{ ...mono, fontWeight: 600 }}>{ERASE_CONFIRM_PHRASE}</span> to confirm. Everything in this portfolio will be deleted.
                  </p>
                  <Input
                    id="privacy-erase-confirm"
                    value={eraseConfirmText}
                    onChange={setEraseConfirmText}
                    placeholder={ERASE_CONFIRM_PHRASE}
                    style={{ ...mono, maxWidth: 280 }}
                  />
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <Button
                      variant="danger"
                      disabled={eraseConfirmText !== ERASE_CONFIRM_PHRASE || erasing}
                      onClick={runErase}
                    >
                      {erasing ? "Deleting…" : "Permanently delete"}
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setErasePanelOpen(false);
                        setEraseConfirmText("");
                      }}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </Card>
        </div>
      </PageBody>
    </>
  );
}

/** One figure from the export summary, labelled. */
function Count({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt
        style={{
          font: "var(--type-label)",
          textTransform: "uppercase",
          letterSpacing: "var(--tracking-label)",
          color: "var(--text-tertiary)",
        }}
      >
        {label}
      </dt>
      <dd
        style={{
          margin: "2px 0 0",
          font: "var(--type-body)",
          fontVariantNumeric: "tabular-nums",
          color: "var(--text-primary)",
        }}
      >
        {value.toLocaleString("en-IN")}
      </dd>
    </div>
  );
}
