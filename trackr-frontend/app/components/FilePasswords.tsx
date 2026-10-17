"use client";

import Link from "next/link";
import { useState, type CSSProperties } from "react";
import { api, ApiError } from "@/lib/api-client";
import type { FilePasswordOut } from "@/lib/api-types";
import { useFetch } from "@/lib/use-fetch";
import { Banner, Button, Input } from "./ds";

/**
 * Settings → File passwords: your PAN, which statement sources have a saved password, and
 * a way to forget one.
 *
 * THE PAN IS TRIED FIRST on every locked file (capitals, then small letters), before any
 * saved password and before anyone is asked — so saving it here is what makes most
 * statements open by themselves. It is set here, never shown back.
 *
 * NO "ADD" HERE, deliberately. A password is saved only on the Import page, after it has
 * actually opened one of the person's files — typed here there would be nothing to prove
 * it right, and a wrong one saved would make every later file fail with no clue why. So
 * this lists, removes, and says where adding happens.
 */
export function FilePasswords({ investorId, token }: { investorId: string | null; token: string | null }) {
  const rows = useFetch<FilePasswordOut[]>(
    investorId && token ? `/investors/${investorId}/file-passwords` : null,
    {},
    token
  );
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pan, setPan] = useState("");
  const [savingPan, setSavingPan] = useState(false);

  async function savePan(e: React.FormEvent) {
    e.preventDefault();
    if (!investorId || pan.trim().length === 0) return;
    setSavingPan(true);
    setError(null);
    try {
      await api.put(`/investors/${investorId}/pan`, { token, json: { pan: pan.trim().toUpperCase() } });
      setPan("");
      rows.refetch();
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 400
          ? "That doesn't look like a PAN. It's ten characters, like ABCDE1234F."
          : err instanceof ApiError
            ? err.message
            : "We couldn't reach the server. Try again."
      );
    } finally {
      setSavingPan(false);
    }
  }

  async function remove(source: string) {
    if (!investorId) return;
    setRemoving(source);
    setError(null);
    try {
      await api.delete(`/investors/${investorId}/file-passwords/${source}`, { token });
      rows.refetch();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't reach the server. Try again.");
    } finally {
      setRemoving(null);
    }
  }

  if (rows.error) {
    return (
      <Banner tone="negative" title="Couldn't load your file passwords">
        {rows.error}
      </Banner>
    );
  }
  if (!rows.data) {
    return <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>Loading…</p>;
  }

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {error && (
        <Banner tone="negative" title="That didn't work">
          {error}
        </Banner>
      )}
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
        {rows.data.filter((row) => row.source === "pan" || row.saved).map((row) => (
          <li key={row.source} style={rowStyle}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <label htmlFor={row.source === "pan" ? "settings-pan" : undefined} style={{ display: "block", margin: 0, font: "var(--type-body-sm)", fontWeight: 500, color: "var(--text-primary)" }}>
                {row.label}
              </label>
              <p style={{ margin: "2px 0 0", font: "var(--type-label)", color: "var(--text-tertiary)" }}>
                {row.hint}
              </p>
            </div>
            <span
              style={{
                font: "var(--type-label)",
                color: row.saved ? "var(--positive)" : "var(--text-tertiary)",
                whiteSpace: "nowrap",
              }}
            >
              {row.saved ? "Saved" : "Not set"}
            </span>
            {row.source === "pan" && (
              <form onSubmit={savePan} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <Input
                  id="settings-pan"
                  value={pan}
                  onChange={(v) => setPan(v.toUpperCase())}
                  placeholder={row.saved ? "Replace PAN" : "ABCDE1234F"}
                  autoComplete="off"
                  style={{ maxWidth: 160, fontFamily: "var(--font-mono)" }}
                />
                <Button variant="secondary" size="sm" type="submit" disabled={savingPan || pan.trim().length === 0}>
                  {savingPan ? "Saving…" : "Save"}
                </Button>
              </form>
            )}
            {row.saved && row.source !== "pan" && (
              <Button
                variant="secondary"
                size="sm"
                disabled={removing === row.source}
                onClick={() => void remove(row.source)}
              >
                {removing === row.source ? "Removing…" : "Remove"}
              </Button>
            )}
          </li>
        ))}
      </ul>
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        {rows.data.some((row) => row.source !== "pan" && row.saved)
          ? "Only passwords you have saved are listed here. "
          : "No statement passwords saved yet. "}
        If a file is locked, enter its password on the <Link href="/import">Import page</Link>.
        It is saved after it successfully opens the file.
      </p>
    </div>
  );
}

const rowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  flexWrap: "wrap",
  padding: "10px 12px",
  border: "1px solid var(--border)",
  background: "var(--surface)",
};
