"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type DragEvent } from "react";
import { api, ApiError, useAuth } from "@trackr/core";
import type { AccountOut, BrokerOut, FileOutcomeOut, FilesUploadOut } from "@trackr/core";
import { Banner, Icon, Input, Select } from "@trackr/ui";

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
 * different file, so it is the one failure that gets an inline form — see PanUnlock.
 */
export function FileUploadStep({
  accounts,
  brokers,
  onStaged,
  onBusyChange,
}: {
  accounts: AccountOut[];
  /** The registry, for per-broker "where do I find these files" steps. */
  brokers: BrokerOut[];
  /** Batch ids with rows waiting. The next step promotes them — this one never writes to
   * the ledger, matching the server route it calls. */
  onStaged: (batchIds: string[]) => void;
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

  /**
   * Files that came back locked, kept so unlocking can retry them without asking anyone
   * to find the same files in the same folder a second time. A browser cannot re-read a
   * file it was given earlier unless we hold the File object, so we hold it — in memory
   * only, for the life of this step.
   */
  const [lockedFiles, setLockedFiles] = useState<File[]>([]);

  useEffect(() => {
    // Default to the only account when there is only one; with several, the picker below
    // is shown and the first is a starting point the person can change.
    if (!accountId && accounts.length > 0) setAccountId(accounts[0]!.id);
  }, [accounts, accountId]);

  useEffect(() => {
    onBusyChange?.(uploading);
  }, [uploading, onBusyChange]);

  const upload = useCallback(
    async (picked: File[]) => {
      if (!investorId || !accountId || picked.length === 0) return;

      const local: Rejected[] = [];
      const sendable: File[] = [];
      for (const file of picked) {
        const problem = tier1(file);
        if (problem) local.push(problem);
        else sendable.push(file);
      }
      setRejectedLocally(local);
      setError(null);
      if (sendable.length === 0) return;

      setUploading(true);
      try {
        const form = new FormData();
        form.set("investor_id", investorId);
        form.set("account_id", accountId);
        for (const file of sendable) form.append("files", file);

        const result = await api.post<FilesUploadOut>("/ingest/files", { formData: form, token });
        // Append rather than replace: adding a second batch of files is a normal thing to
        // do, and wiping the first batch's receipts when it happens would read as failure.
        setOutcomes((previous) => [
          ...previous.filter((o) => !result.files.some((f) => f.filename === o.filename)),
          ...result.files,
        ]);
        // Hold on to anything that came back locked so PanUnlock can retry it directly.
        const lockedNames = new Set(result.files.filter((f) => f.encrypted).map((f) => f.filename));
        setLockedFiles(sendable.filter((f) => lockedNames.has(f.name)));
        if (result.staged_batch_ids.length > 0) onStaged(result.staged_batch_ids);
      } catch (err) {
        setError(friendly(err));
      } finally {
        setUploading(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [investorId, accountId, token, onStaged]
  );

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void upload(Array.from(event.dataTransfer.files ?? []));
  }

  const imported = outcomes.filter((o) => o.ok);
  const rejected = outcomes.filter((o) => !o.ok);
  const trades = imported.reduce((sum, o) => sum + o.parsed, 0);
  const locked = rejected.filter((o) => o.encrypted);

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
        </Banner>
      )}

      {accounts.length > 1 && (
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
      )}

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
        }}
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 6,
          padding: "28px 20px",
          border: "1px dashed " + (dragging ? "var(--ink-900)" : "var(--border-strong)"),
          background: dragging ? "var(--surface-hover)" : "var(--surface-sunken)",
          cursor: uploading ? "progress" : "pointer",
          textAlign: "center",
          transition: "background-color var(--duration-fast) ease, border-color var(--duration-fast) ease",
        }}
      >
        <Icon name="import" size={22} style={{ color: "var(--text-tertiary)" }} />
        <p
          style={{
            margin: 0,
            font: "var(--type-body)",
            fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
            color: "var(--text-primary)",
          }}
        >
          {uploading ? "Reading your files…" : "Drop your broker files here"}
        </p>
        <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)", maxWidth: "46ch" }}>
          Pick as many as you like, all at once. Contract notes, tradebooks, PDFs or
          spreadsheets. We work out what each one is.
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.csv,.xlsx"
          onChange={(e) => void upload(Array.from(e.target.files ?? []))}
          style={{ display: "none" }}
        />
      </div>

      {locked.length > 0 && (
        <PanUnlock
          filenames={locked.map((o) => o.filename)}
          brokerName={selected ? brokerNameOf(brokers, selected.broker) : "Your broker"}
          passwordRule={selected ? passwordRuleOf(brokers, selected.broker) : ""}
          onUnlocked={() => {
            // Same files, same account, now with a password on file. Clearing their
            // outcomes first stops the old "locked" rows sitting under the new result.
            const retry = lockedFiles;
            setOutcomes((previous) => previous.filter((o) => !o.encrypted));
            setLockedFiles([]);
            void upload(retry);
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

      {trades > 0 && (
        <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
          {trades.toLocaleString("en-IN")} trade{trades === 1 ? "" : "s"} ready. Add more files
          for a longer history, or continue.
        </p>
      )}

      <WhereToFindFiles accounts={accounts} brokers={brokers} />
    </div>
  );
}

/**
 * A password-protected PDF: the one rejection whose fix is a field, not a different file.
 *
 * ASKED HERE RATHER THAN ON THE SIGNUP FORM, deliberately. A PAN is sensitive, and a PAN
 * field on registration is a sensitive question asked before its purpose is visible — and
 * asked of every person, including the ones importing a tradebook CSV who will never need
 * it. Here its purpose is on screen: a named file we could not open, and one sentence
 * saying why. The server already told us this is the case (`encrypted`, set only for a PDF
 * we could not open because it is locked — distinct from an unreadable one), so this form
 * appears exactly when it is the answer and never otherwise.
 *
 * The password rule comes from the broker registry, which derives it from the transform
 * the parser actually applies — docs/INGESTION.md calls per-broker PAN casing "a real
 * trap", and a hint that could disagree with the code would be the same trap again.
 */
function PanUnlock({
  filenames,
  brokerName,
  passwordRule,
  onUnlocked,
}: {
  filenames: string[];
  brokerName: string;
  passwordRule: string;
  onUnlocked: () => void;
}) {
  const { investorId, token } = useAuth();
  const [pan, setPan] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function unlock() {
    if (!investorId || pan.trim().length === 0) return;
    setSaving(true);
    setError(null);
    try {
      await api.put(`/investors/${investorId}/pan`, { token, json: { pan: pan.trim().toUpperCase() } });
      setPan("");
      onUnlocked();
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 400
          ? "That doesn't look like a PAN. It's ten characters, like ABCDE1234F."
          : friendly(err)
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: "12px 14px",
        border: "1px solid var(--border)",
        borderLeft: "2px solid var(--warning)",
        background: "var(--surface)",
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
        {filenames.length === 1 ? `${filenames[0]} is` : `${filenames.length} of these files are`}{" "}
        password-protected
      </p>
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
        {brokerName} locks these files. The password is{" "}
        {(passwordRule || "Your PAN").replace(/^Your/, "your")}. Add it once and we will open
        this file and every future one. We keep it encrypted, use it only to open your
        documents, and never show it back to you.
      </p>
      {error && (
        <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--negative)" }}>{error}</p>
      )}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <Input
          id="unlock-pan"
          value={pan}
          onChange={(v) => setPan(v.toUpperCase())}
          placeholder="ABCDE1234F"
          style={{ maxWidth: 180, fontFamily: "var(--font-mono)" }}
        />
        <button
          type="button"
          onClick={() => void unlock()}
          disabled={saving || pan.trim().length === 0}
          style={{
            padding: "8px 16px",
            border: "1px solid var(--ink-900)",
            borderRadius: "var(--radius-md)",
            background: "var(--ink-900)",
            color: "var(--text-inverse)",
            font: "var(--type-body-sm)",
            fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
            cursor: saving || pan.trim().length === 0 ? "not-allowed" : "pointer",
            opacity: saving || pan.trim().length === 0 ? 0.5 : 1,
          }}
        >
          {saving ? "Unlocking…" : "Unlock"}
        </button>
      </div>
    </div>
  );
}

/** What an accepted file turned out to be, said in the person's words rather than the
 * adapter's. `document` is the server's classification, so this never guesses. */
function describe(outcome: FileOutcomeOut): string {
  const kind =
    outcome.document === "tradebook_csv"
      ? "Tradebook"
      : outcome.document === "contract_note_pdf" || outcome.document === "contract_note_xlsx"
        ? "Contract note"
        : "File";
  if (outcome.parsed === 0) return `${kind}: no new trades in this one`;
  return `${kind}: ${outcome.parsed.toLocaleString("en-IN")} trade${outcome.parsed === 1 ? "" : "s"} found`;
}

function OutcomeRow({
  tone,
  filename,
  headline,
  detail,
}: {
  tone: "ok" | "bad";
  filename: string;
  headline: string;
  detail: string | null;
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
        {detail && (
          <p style={{ margin: "2px 0 0", font: "var(--type-label)", color: "var(--text-tertiary)" }}>{detail}</p>
        )}
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

function brokerNameOf(brokers: BrokerOut[], code: string): string {
  return brokers.find((b) => b.code === code.trim().toUpperCase())?.name ?? code;
}

function passwordRuleOf(brokers: BrokerOut[], code: string): string {
  return brokers.find((b) => b.code === code.trim().toUpperCase())?.pdf_password ?? "";
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
