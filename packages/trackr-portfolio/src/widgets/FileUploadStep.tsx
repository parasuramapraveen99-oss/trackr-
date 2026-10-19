"use client";

import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type DragEvent } from "react";
import { api, ApiError, broadcastPortfolioRefresh, useAuth } from "@trackr/core";
import type { AccountOut, BrokerOut, FileOutcomeOut, FilePasswordOut, FilesUploadOut } from "@trackr/core";
import { Banner, Icon, Input, Select } from "@trackr/ui";
import { ImportIllustration, UploadIllustration } from "./ImportIllustration";
import { TradebookGuide } from "./TradebookGuide";
import visual from "./ImportExperience.module.css";

/**
 * Tier 1, client-side copy.
 *
 * The server runs this same check (app/ingest/file_validation.py::check_extension) and the
 * server's copy is the one that counts — `accept` on a file input is a picker hint, and a
 * drag-and-drop bypasses it entirely. This copy exists only so the answer is instant: a
 * person who dragged in a folder of holiday photos should not have to upload 40MB to be
 * told.
 */
const ALLOWED = ["pdf", "csv", "xlsx"] as const;

/** 25MB. Well above a five-year contract-note export and well below anything that would
 * sit in a browser for a minute before the server got a chance to say no. */
const MAX_BYTES = 25 * 1024 * 1024;

interface Rejected {
  filename: string;
  reason: string;
  hint: string;
}

function extensionOf(name: string): string {
  const trimmed = name.trim();
  return trimmed.includes(".") ? trimmed.split(".").pop()!.toLowerCase() : "";
}

function tier1(file: File): Rejected | null {
  const ext = extensionOf(file.name);
  if (!ALLOWED.includes(ext as (typeof ALLOWED)[number])) {
    return {
      filename: file.name,
      reason: ext ? `We can't read .${ext} files.` : "This file has no extension.",
      hint: "Upload a PDF, CSV or XLSX downloaded from your broker.",
    };
  }
  if (file.size === 0) {
    return {
      filename: file.name,
      reason: "This file is empty.",
      hint: "Download it again from your broker and try once more.",
    };
  }
  if (file.size > MAX_BYTES) {
    return {
      filename: file.name,
      reason: `This file is ${(file.size / 1024 / 1024).toFixed(0)}MB, which is larger than we accept.`,
      hint: "Export a shorter date range. One financial year per file works well.",
    };
  }
  return null;
}

/**
 * Step 3 — hand over the files, in any mix, from any broker.
 *
 * WHY THERE IS NO "STATEMENT TYPE" PICKER. The import screen has one, and it is right
 * there: someone who came to import a tradebook knows they have a tradebook. A person
 * setting up for the first time has a downloads folder and no idea which of those files is
 * a "tradebook" and which is a "contract note" — asking them to sort it is asking them to
 * learn our vocabulary before they can see their own portfolio. So everything goes into
 * one drop zone and POST /ingest/files decides per file, by reading it.
 *
 * WHY THERE *IS* AN ACCOUNT PICKER, but only sometimes. Files used to land in
 * `accounts[0]` unconditionally — fine when the wizard only ever made one account, wrong
 * the moment it can make several. The honest rule is: with one account there is nothing to
 * ask, and with several we ask rather than guess. Automatic routing by the client code
 * printed on the document is the better answer and is a separate, named piece of work
 * (docs/ONBOARDING_AS_IS_VS_TO_BE.md §2 screen 3); until it lands, one dropdown and one
 * click beats a wrong guess nobody is told about.
 *
 * TWO TIERS, AND WHAT EACH IS FOR. Tier 1 (above) is instant and about the name. Tier 2
 * happens on the server and is about the bytes — it is what catches a contract note
 * renamed `.csv`, a scanned PDF with no text in it, a CAS where a tradebook was needed.
 * Neither tier ever produces a stack trace here: a rejected file gets one sentence saying
 * what it is and one saying what to do instead, and the other files still import.
 *
 * AND ONE SPECIAL REJECTION: a locked PDF. That is the one failure whose fix is not a
 * different file, so it is the one failure that gets an inline form — see
 * FilePasswordUnlock.
 */
export function FileUploadStep({
  accounts,
  brokers,
  onStaged,
  onUploaded,
  onBusyChange,
  compact = false,
}: {
  accounts: AccountOut[];
  /** Keep the illustrated introduction in onboarding; the Import page uses a compact picker. */
  compact?: boolean;
  /** The registry, for per-broker "where do I find these files" steps. */
  brokers: BrokerOut[];
  /** Trade batches awaiting review. CAS balances are processed without trade promotion. */
  onStaged: (batchIds: string[]) => void;
  /** Refresh import history even when a CAS produces no trade rows. */
  onUploaded?: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const { investorId, token } = useAuth();
  const [accountId, setAccountId] = useState<string>("");
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<FileOutcomeOut[]>([]);
  const [rejectedLocally, setRejectedLocally] = useState<Rejected[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [failedFiles, setFailedFiles] = useState<File[]>([]);

  /**
   * Files that came back locked, kept so unlocking can retry them without asking anyone
   * to find the same files in the same folder a second time. A browser cannot re-read a
   * file it was given earlier unless we hold the File object, so we hold it — in memory
   * only, for the life of this step.
   */
  const [lockedFiles, setLockedFiles] = useState<Record<string, File>>({});
  /** The statement sources a password can be saved for, with labels — from the server so
   * the dropdown can never offer a source it would refuse. */
  const [sources, setSources] = useState<FilePasswordOut[]>([]);
  /** Files whose last typed password did not open them. Cleared when they open. */
  const [wrongPassword, setWrongPassword] = useState<Record<string, boolean>>({});
  /** Loading the password sources failed. Its own message: nothing was uploaded, so it
   * must not read as a failed upload. */
  const [sourcesError, setSourcesError] = useState<string | null>(null);

  useEffect(() => {
    if (!investorId) return;
    let cancelled = false;
    api
      .get<FilePasswordOut[]>(`/investors/${investorId}/file-passwords`, { token })
      .then((rows) => {
        if (!cancelled) setSources(rows);
      })
      .catch((err) => {
        if (!cancelled)
          setSourcesError(
            err instanceof ApiError && err.status === 404
              ? "The server doesn't have file passwords yet: it is running older code than this page. Restart or redeploy the backend."
              : friendly(err)
          );
      });
    return () => {
      cancelled = true;
    };
  }, [investorId, token]);

  useEffect(() => {
    // Default to the only account when there is only one; with several, the picker below
    // is shown and the first is a starting point the person can change.
    if (!accountId && accounts.length > 0) setAccountId(accounts[0]!.id);
  }, [accounts, accountId]);

  useEffect(() => {
    onBusyChange?.(uploading);
  }, [uploading, onBusyChange]);

  const upload = useCallback(
    async (picked: File[], typed?: { source: string; password: string }) => {
      if (!investorId || !accountId || picked.length === 0 || inFlight.current) return;

      const local: Rejected[] = [];
      const sendable: File[] = [];
      for (const file of picked) {
        const problem = tier1(file);
        if (problem) local.push(problem);
        else sendable.push(file);
      }
      setRejectedLocally((previous) => [
        ...previous.filter((item) => !picked.some((file) => file.name === item.filename)),
        ...local,
      ]);
      setError(null);
      if (sendable.length === 0) return;

      inFlight.current = true;
      setPendingFiles(sendable);
      setFailedFiles([]);
      setUploading(true);
      try {
        const form = new FormData();
        form.set("investor_id", investorId);
        form.set("account_id", accountId);
        for (const file of sendable) form.append("files", file);
        if (typed) {
          // Tried on these files exactly as typed, and saved only if it opens one.
          form.set("password", typed.password);
          form.set("password_source", typed.source);
        }

        const result = await api.post<FilesUploadOut>("/ingest/files", { formData: form, token });
        // Append rather than replace: adding a second batch of files is a normal thing to
        // do, and wiping the first batch's receipts when it happens would read as failure.
        setOutcomes((previous) => [
          ...previous.filter((o) => !result.files.some((f) => f.filename === o.filename)),
          ...result.files,
        ]);
        // Hold on to anything that came back locked so its unlock box can retry it
        // directly; forget anything that has now opened.
        setLockedFiles((previous) => {
          const next = { ...previous };
          for (const file of sendable) {
            const outcome = result.files.find((f) => f.filename === file.name);
            if (outcome?.encrypted) next[file.name] = file;
            else delete next[file.name];
          }
          return next;
        });
        setWrongPassword((previous) => {
          const next = { ...previous };
          for (const outcome of result.files) next[outcome.filename] = outcome.wrong_password;
          return next;
        });
        if (result.staged_batch_ids.length > 0) onStaged(result.staged_batch_ids);
        onUploaded?.();
        if (result.files.some((file) => file.ok)) broadcastPortfolioRefresh();
        return result;
      } catch (err) {
        setError(friendly(err));
        setFailedFiles(sendable);
      } finally {
        inFlight.current = false;
        setPendingFiles([]);
        setUploading(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [investorId, accountId, token, onStaged, onUploaded]
  );

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void upload(Array.from(event.dataTransfer.files ?? []));
  }

  const imported = outcomes.filter((o) => o.ok);
  // Locked files get their own unlock box below, not a plain "rejected" row.
  const rejected = outcomes.filter((o) => !o.ok && !o.encrypted);
  const locked = outcomes.filter((o) => !o.ok && o.encrypted && lockedFiles[o.filename]);

  const selected = accounts.find((a) => a.id === accountId);

  if (accounts.length === 0) {
    // Step 2 creates at least one. Landing here without any means someone jumped the
    // flow, and saying so beats a disabled drop zone with no explanation.
    return (
      <Banner tone="warning" title="No account to put these in yet">
        Open Brokers at the top and pick the brokers you use. Your files need somewhere to
        land.
      </Banner>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {error && (
        <Banner tone="negative" title="That upload didn't go through">
          {error}
          {failedFiles.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <ul style={{ paddingLeft: 18, overflowWrap: "anywhere" }}>
                {failedFiles.map((file, index) => <li key={index}>{file.name} · Upload not confirmed</li>)}
              </ul>
              <button type="button" disabled={uploading} onClick={() => void upload(failedFiles)}>
                Retry upload
              </button>
            </div>
          )}
        </Banner>
      )}

      {sourcesError && (
        <Banner tone="negative" title="Couldn't load your saved file passwords">
          {sourcesError} You can still upload files; locked ones can't be unlocked until this is fixed.
        </Banner>
      )}

      {accounts.length > 1 && (
        <fieldset disabled={uploading || locked.length > 0} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
              font: "var(--type-body-sm)",
              color: "var(--text-secondary)",
            }}
          >
            These files are from
            <Select
              id="upload-account"
              value={accountId}
              onChange={setAccountId}
              options={accounts.map((a) => ({
                value: a.id,
                label: a.label ? `${a.label} (${a.broker})` : a.broker,
              }))}
            />
            <span style={{ color: "var(--text-tertiary)" }}>
              Upload one broker&apos;s files, then switch and do the next.
            </span>
          </label>
        </fieldset>
      )}

      <div className={visual.experience}>
        <div className={compact ? visual.compactLayout : visual.layout}>
          {!compact && <div className={visual.intro}>
            <ImportIllustration />
            <span className={visual.eyebrow}>Your portfolio starts here</span>
            <h3>A few files.<br />Your whole investment story.</h3>
            <p>Bring your stock contract notes, mutual fund CAS and tradebooks together. Drop your files beside this panel to get started.</p>
            <div className={visual.fileTypes}><span>Contract notes</span><span>Mutual fund CAS</span><span>Tradebooks</span></div>
          </div>}
          <div className={compact ? visual.compactUploadSide : visual.uploadSide}>
            {!compact && <div className={visual.uploadHeading}><Icon name="plus" size={16} /> New import</div>}
            <div
              className={`${visual.dropZone} ${compact ? visual.compactDropZone : ""} ${dragging ? visual.dragging : ""}`}
              onDragOver={(e) => { e.preventDefault(); if (!uploading) setDragging(true); }}
              onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false); }}
              onDrop={onDrop}
              onClick={() => { if (!uploading) inputRef.current?.click(); }}
              role="button"
              aria-label={uploading ? "Reading your files" : "Upload broker statements"}
              tabIndex={uploading ? -1 : 0}
              aria-disabled={uploading}
              aria-busy={uploading}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  if (!uploading) inputRef.current?.click();
                }
              }}
            >
              {!compact && <UploadIllustration />}
              <span className={visual.uploadButton}><Icon name="import" size={16} />{uploading ? "Reading your files…" : "Choose files"}</span>
              <p>or drag and drop files here</p>
              <input ref={inputRef} type="file" disabled={uploading} aria-label="Upload statements" multiple accept=".pdf,.csv,.xlsx" onChange={(e) => void upload(Array.from(e.target.files ?? []))} style={{ display: "none" }} />
            </div>
            <span className={visual.formatHint}>PDF, CSV or XLSX · Up to 25 MB per file<br />Select several files in one go. We identify each one.</span>
          </div>
        </div>
        {!compact && <div className={visual.journey}><span><b>1</b> Choose files</span><span>→</span><span><b>2</b> We read them</span><span>→</span><span><b>3</b> Review your imports</span></div>}
      </div>

      {uploading && (
        <div role="status" aria-live="polite" style={{ display: "grid", gap: 10, padding: 14, background: "var(--surface-sunken)", border: "1px solid var(--border)" }}>
          <style>{`@keyframes trackr-upload-spin { to { transform: rotate(360deg); } } @media (prefers-reduced-motion: reduce) { .trackr-upload-spinner { animation: none !important; } }`}</style>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span aria-hidden="true" className="trackr-upload-spinner" style={{ width: 18, height: 18, flexShrink: 0, border: "2px solid var(--border-strong)", borderTopColor: "var(--text-primary)", borderRadius: "50%", animation: "trackr-upload-spin 0.8s linear infinite" }} />
            <strong style={{ font: "var(--type-body-sm)" }}>Uploading and processing {pendingFiles.length} file{pendingFiles.length === 1 ? "" : "s"}…</strong>
          </div>
          <ul style={{ margin: 0, paddingLeft: 18, font: "var(--type-body-sm)", color: "var(--text-secondary)", overflowWrap: "anywhere" }}>
            {pendingFiles.map((file, index) => <li key={index}>{file.name} · Processing</li>)}
          </ul>
          <span style={{ font: "var(--type-label)", color: "var(--text-tertiary)" }}>Keep this page open until the results appear.</span>
        </div>
      )}

      {locked.length > 0 && (
        <FilePasswordUnlock
          filenames={locked.map((outcome) => outcome.filename)}
          sources={sources}
          initialSource={guessSource(locked[0]!.filename, selected?.broker ?? "", sources)}
          wrongPassword={locked.some((outcome) => wrongPassword[outcome.filename])}
          busy={uploading}
          onSubmit={async (source, password) => {
            await upload(locked.map((outcome) => lockedFiles[outcome.filename]!), { source, password });
          }}
        />
      )}

      {(imported.length > 0 || rejected.length > 0 || rejectedLocally.length > 0) && (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {imported.map((outcome) => (
            <OutcomeRow
              key={outcome.filename}
              tone="ok"
              filename={outcome.filename}
              headline={describe(outcome)}
              detail={outcome.notes.join(" ") || null}
              collapsibleDetail={outcome.document === "cas_pdf" && outcome.rejected === 0}
            />
          ))}
          {rejected.map((outcome) => (
            <OutcomeRow
              key={outcome.filename}
              tone="bad"
              filename={outcome.filename}
              headline={outcome.reason ?? "We couldn't read this file."}
              detail={outcome.hint}
            />
          ))}
          {rejectedLocally.map((item) => (
            <OutcomeRow
              key={item.filename}
              tone="bad"
              filename={item.filename}
              headline={item.reason}
              detail={item.hint}
            />
          ))}
        </ul>
      )}


      <TradebookGuide accounts={accounts} brokers={brokers} />
      {!compact && <WhereToFindFiles accounts={accounts} brokers={brokers} />}
    </div>
  );
}

/** One password is tried against every remaining locked file in this upload. Files
 * with another password stay listed for a separate retry; successful ones are removed. */
function FilePasswordUnlock({
  filenames,
  sources,
  initialSource,
  wrongPassword,
  busy,
  onSubmit,
}: {
  filenames: string[];
  sources: FilePasswordOut[];
  initialSource: string;
  wrongPassword: boolean;
  busy: boolean;
  onSubmit: (source: string, password: string) => Promise<void>;
}) {
  const formId = useId();
  const [source, setSource] = useState(initialSource);
  const [password, setPassword] = useState("");
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    if (!source && initialSource) setSource(initialSource);
  }, [initialSource, source]);

  const hint = sources.find((s) => s.source === source)?.hint ?? "";
  const disabled = busy || password.length === 0 || !source;

  async function unlock() {
    if (disabled) return;
    setSubmitted(true);
    await onSubmit(source, password);
    setPassword("");
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: "12px 14px",
        border: "1px solid var(--border)",
        borderLeft: `2px solid ${wrongPassword ? "var(--negative)" : "var(--warning)"}`,
        background: "var(--surface)",
      }}
    >
      <p
        style={{
          margin: 0,
          font: "var(--type-body-sm)",
          fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
          color: "var(--text-primary)",
          overflowWrap: "anywhere",
        }}
      >
        {filenames.length} locked file{filenames.length === 1 ? "" : "s"} · One password for all
      </p>
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
        Enter the shared password once. Any files it doesn&apos;t open stay here so you can
        try another password. A verified password is saved encrypted for the selected statement source.
      </p>
      <ul style={{ margin: 0, paddingLeft: 18, font: "var(--type-body-sm)", color: "var(--text-secondary)", overflowWrap: "anywhere" }}>
        {filenames.map((filename, index) => <li key={index}>{filename}</li>)}
      </ul>
      {wrongPassword && submitted && (
        <p role="alert" style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--negative)" }}>
          That password didn&apos;t open the remaining files. Try their password below.
        </p>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void unlock();
        }}
        style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}
      >
        <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
          <label htmlFor={`${formId}-source`} style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>Statement source</label>
          <Select
            id={`${formId}-source`}
            value={source}
            onChange={setSource}
            options={sources
              .filter((s) => s.source !== "pan")
              .map((s) => ({ value: s.source, label: s.label }))}
          />
          <label htmlFor={`${formId}-password`} style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>Statement password</label>
          <Input
            id={`${formId}-password`}
            disabled={busy}
            type="password"
            value={password}
            onChange={setPassword}
            placeholder="Password"
            // "new-password" stops the browser filling in the Trackr+ login password here.
            autoComplete="new-password"
            style={{ maxWidth: 200 }}
          />
          <button
            type="submit"
            disabled={disabled}
            style={{
              padding: "8px 16px",
              border: "1px solid var(--ink-900)",
              borderRadius: "var(--radius-md)",
              background: "var(--ink-900)",
              color: "var(--text-inverse)",
              font: "var(--type-body-sm)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              cursor: disabled ? "not-allowed" : "pointer",
              opacity: disabled ? 0.5 : 1,
            }}
          >
            {busy ? "Unlocking…" : "Unlock files"}
          </button>
        </fieldset>
      </form>
      {hint && (
        <p style={{ margin: 0, font: "var(--type-label)", color: "var(--text-tertiary)" }}>{hint}</p>
      )}
      {sources.some((s) => s.source === "pan" && !s.saved) && (
        <p style={{ margin: 0, font: "var(--type-label)", color: "var(--text-tertiary)" }}>
          Tip: save your PAN in Settings → File passwords and most statements will open by themselves.
        </p>
      )}
    </div>
  );
}

/**
 * Which statement a locked file probably is, from its name alone — a starting choice for
 * the dropdown, never a decision: the person can change it, and the server only saves the
 * password once it has actually opened the file.
 */
function guessSource(filename: string, broker: string, sources: FilePasswordOut[]): string {
  const known = new Set(sources.filter((s) => s.source !== "pan").map((s) => s.source));
  const pick = (code: string) => (known.has(code) ? code : "");
  const name = filename.toLowerCase();
  if (/_txn(\s*\(\d+\))?\.pdf$/.test(name) || name.includes("cdsl")) return pick("cdsl");
  if (name.includes("nsdl")) return pick("nsdl");
  if (name.includes("cams")) return pick("cams");
  if (name.includes("kfin") || name.includes("karvy")) return pick("kfintech");
  return pick(broker.trim().toLowerCase()) || [...known][0] || "";
}

/** What an accepted file turned out to be, said in the person's words rather than the
 * adapter's. `document` is the server's classification, so this never guesses. */
function describe(outcome: FileOutcomeOut): string {
  if (outcome.document === "cas_pdf" && outcome.rejected === 0) return "Processed successfully";
  const kind =
    outcome.document === "tradebook_csv"
      ? "Tradebook"
      : outcome.document === "contract_note_pdf" || outcome.document === "contract_note_xlsx"
        ? "Contract note"
        : "File";
  if (outcome.rejected > 0) return `${kind}: ${outcome.parsed.toLocaleString("en-IN")} trade${outcome.parsed === 1 ? "" : "s"} found · ${outcome.rejected.toLocaleString("en-IN")} row${outcome.rejected === 1 ? " needs" : "s need"} review`;
  if (outcome.parsed === 0) return `${kind}: no new trades in this one`;
  return `${kind}: ${outcome.parsed.toLocaleString("en-IN")} trade${outcome.parsed === 1 ? "" : "s"} found`;
}

function OutcomeRow({
  tone,
  filename,
  headline,
  detail,
  collapsibleDetail = false,
}: {
  tone: "ok" | "bad";
  filename: string;
  headline: string;
  detail: string | null;
  collapsibleDetail?: boolean;
}) {
  const accent = tone === "ok" ? "var(--positive)" : "var(--warning)";
  return (
    <li
      style={{
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
        padding: "10px 12px",
        border: "1px solid var(--border)",
        borderLeft: `2px solid ${accent}`,
        background: "var(--surface)",
      }}
    >
      <span style={{ color: accent, marginTop: 2 }}>
        <Icon name={tone === "ok" ? "check" : "reconcile"} size={14} />
      </span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <p
          style={{
            margin: 0,
            font: "var(--type-body-sm)",
            fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
            color: "var(--text-primary)",
            overflowWrap: "anywhere",
          }}
        >
          {filename}
        </p>
        <p style={{ margin: "2px 0 0", font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
          {headline}
        </p>
        {detail && (collapsibleDetail ? (
          <details style={{ marginTop: 4 }}>
            <summary style={{ cursor: "pointer", font: "var(--type-label)", color: "var(--text-tertiary)" }}>
              Details
            </summary>
            <p style={{ margin: "6px 0 0", font: "var(--type-label)", color: "var(--text-tertiary)", overflowWrap: "anywhere" }}>{detail}</p>
          </details>
        ) : (
          <p style={{ margin: "2px 0 0", font: "var(--type-label)", color: "var(--text-tertiary)" }}>{detail}</p>
        ))}
      </div>
    </li>
  );
}

/**
 * Where the files live, per broker the person actually said they use.
 *
 * This used to name Zerodha Console and nothing else, which is unhelpful in exactly the
 * proportion that the product is multi-broker. The steps come from `GET /brokers`, so a
 * broker added to the registry gets its instructions here for free and none of them can
 * describe a console that does not exist.
 */
function WhereToFindFiles({ accounts, brokers }: { accounts: AccountOut[]; brokers: BrokerOut[] }) {
  const mine = brokers.filter((b) =>
    accounts.some((a) => a.broker.trim().toUpperCase() === b.code)
  );
  if (mine.length === 0) return null;

  return (
    <details style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      <summary style={{ cursor: "pointer", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        Where do I find these files?
      </summary>
      <div style={{ display: "grid", gap: 14, marginTop: 10 }}>
        {mine.map((broker) => (
          <div key={broker.code}>
            {mine.length > 1 && (
              <p
                style={{
                  margin: "0 0 4px",
                  font: "var(--type-label)",
                  textTransform: "uppercase",
                  letterSpacing: "var(--tracking-label)",
                  color: "var(--text-tertiary)",
                }}
              >
                {broker.name}
              </p>
            )}
            <ol
              style={{
                margin: 0,
                paddingLeft: 18,
                display: "grid",
                gap: 6,
                font: "var(--type-body-sm)",
                color: "var(--text-secondary)",
              }}
            >
              {broker.console_steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </div>
        ))}
        <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          Drag everything you downloaded into the box above, in one go.
        </p>
      </div>
    </details>
  );
}

function friendly(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return "You need to give permission first. Open Permission at the top.";
    if (err.status === 413) return "Those files are too large together. Try a few at a time.";
    if (err.status >= 500) return "Our server had trouble reading those. Try again in a moment.";
    return err.message;
  }
  return "We couldn't reach the server. Check your connection and try again.";
}
