"use client";

import { useState, type CSSProperties, type FormEvent } from "react";
import { api, ApiError, GRIEVANCE_OFFICER } from "@trackr/core";
import { Banner, Button, Field, Input } from "@trackr/ui";

/** POST /grievances response (trackr-backend/app/schemas/grievance.py GrievanceOut). */
interface GrievanceFiled {
  id: string;
  reference: string;
  status: string;
  raised_at: string;
  due_at: string;
}

const MAX_SUBJECT = 200;
const MAX_MESSAGE = 5000;

/**
 * The public complaint form behind /grievance in both apps. No sign-in: a complaint must be
 * possible from someone who has no account, or who cannot get into theirs.
 *
 * WHY NAME AND EMAIL GO INTO `detail`. The intake schema (`GrievanceIn`) has no contact
 * fields, and a signed-out complainant who leaves no way to reply cannot be answered. Rather
 * than a schema change, the form requires both and writes them as the first lines of
 * `detail`, where the Grievance Officer reads them. A contact column is the cleaner fix
 * later (docs/legal/LEGAL_REVIEW.md, 3.6).
 */
export function GrievanceForm({ channel }: { channel: string }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filed, setFiled] = useState<GrievanceFiled | null>(null);

  const emailLooksValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const ready =
    name.trim().length > 0 && emailLooksValid && subject.trim().length > 0 && message.trim().length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const row = await api.post<GrievanceFiled>("/grievances", {
        json: {
          subject: subject.trim().slice(0, MAX_SUBJECT),
          channel,
          detail:
            `From: ${name.trim()} <${email.trim()}>\n\n` + message.trim().slice(0, MAX_MESSAGE),
        },
      });
      setFiled(row);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status < 500
          ? err.message
          : "We couldn’t file your complaint just now. Please try again in a moment."
      );
    } finally {
      setBusy(false);
    }
  }

  if (filed) {
    return (
      <Banner tone="positive" title={`Complaint filed: reference ${filed.reference}`}>
        Keep this reference. We will acknowledge your complaint at {email.trim()} within{" "}
        {GRIEVANCE_OFFICER.acknowledgeHours} hours and aim to resolve it within{" "}
        {GRIEVANCE_OFFICER.resolveDays} days.
      </Banner>
    );
  }

  return (
    <form onSubmit={(event) => void submit(event)} style={{ display: "grid", gap: 14 }}>
      {error && (
        <Banner tone="negative" title="Not sent">
          {error}
        </Banner>
      )}
      <Field label="Your name" htmlFor="grievance-name">
        <Input id="grievance-name" value={name} onChange={setName} autoComplete="name" required />
      </Field>
      <Field label="Email, so we can reply" htmlFor="grievance-email">
        <Input
          id="grievance-email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          required
        />
      </Field>
      <Field label="Subject" htmlFor="grievance-subject">
        <Input id="grievance-subject" value={subject} onChange={(v) => setSubject(v.slice(0, MAX_SUBJECT))} required />
      </Field>
      <Field
        label="What happened"
        htmlFor="grievance-message"
        hint="Please don’t include your PAN or account passwords."
      >
        <textarea
          id="grievance-message"
          value={message}
          required
          rows={6}
          maxLength={MAX_MESSAGE}
          onChange={(e) => setMessage(e.target.value)}
          style={textareaStyle}
        />
      </Field>
      <div>
        <Button type="submit" disabled={!ready || busy}>
          {busy ? "Sending…" : "Send complaint"}
        </Button>
      </div>
    </form>
  );
}

const textareaStyle: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "8px 12px",
  borderRadius: "var(--radius-md)",
  border: "1px solid var(--border)",
  background: "var(--surface)",
  color: "var(--text-primary)",
  font: "var(--type-body)",
  resize: "vertical",
};
