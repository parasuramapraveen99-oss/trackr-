"use client";

import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { AccountOut, BrokerOut } from "@/lib/api-types";
import { Banner, Checkbox } from "../ds";

/**
 * Step 2 — where do you trade? A screen that did not exist.
 *
 * WHAT USED TO HAPPEN INSTEAD. Nothing asked. The permission step silently created one
 * account with `broker: "ZERODHA"`, so a Groww user was handed a Zerodha account and
 * never told, every file landed in it, and a multi-broker investor had to find the raw
 * form on the Settings page by themselves — the wizard never mentioned that more than one
 * account was possible (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loopholes L7 and L15).
 *
 * NOTHING IN THE DATABASE HAD TO CHANGE for this. `account` has always been a plain
 * many-to-one on `investor`, PDF passwords have always been per-broker, and forwarding
 * addresses have always been derived from the adapter registry. What was missing was a
 * question, and honesty about the answers.
 *
 * A CHECKBOX ONLY WHERE THERE IS A CHOICE. Per-broker capability badges (notes/book/files/
 * email) used to sit beside every row and are gone. A row now carries a tickable checkbox,
 * or a tag saying why it has none: "Coming soon" for a broker whose documents nothing here
 * parses yet, "already added" for one this investor already has an account with.
 *
 * NOTHING ARRIVES PRE-TICKED, which is the point. An existing account used to render as a
 * ticked, locked checkbox, and a ticked box on a screen asking you to choose reads as a
 * choice already made on your behalf. It is a fact about the account, so it is now stated as
 * one. Every tick on this screen was made by the person looking at it.
 *
 * THE TAGS ARE DERIVED, NEVER TYPED HERE. "Coming soon" is `documents` arriving empty from
 * `GET /brokers`, which app/brokers.py inverts out of the real upload routing table. So the
 * day a Groww parser lands, that row grows a checkbox with no edit to this file, and no edit
 * to this file can ever promise a parser that does not exist. docs/ONBOARDING_FLOW.md Part 4
 * warns that a picker listing brokers it silently fails on turns a clear "we only do Zerodha
 * today" into a failure at upload time; a row with no checkbox is that warning taken
 * literally.
 *
 * AND THERE IS NO FREE-TEXT ROW. It used to accept any broker name at all. Settings'
 * AccountOnboarding still does, and is now the one place an unlisted broker is added. Here
 * the answer for someone who uses a broker we cannot read yet is to skip: the dashboard
 * checklist keeps the step open, and nothing about looking around an empty dashboard needs
 * an account first.
 *
 * ADDING, NEVER REMOVING. Unticking a broker you already have an account with does not
 * delete it. Accounts own transactions, and a checkbox is not a safe place to put "erase
 * a broker's entire history" — deleting one is a deliberate act that belongs on the
 * Privacy page, where it is spelled out. Existing accounts render ticked and locked.
 */
export function BrokerPickerStep({
  onAccountsChanged,
  onReadyChange,
}: {
  /** Fires after accounts are created, so the wizard can advance and the next step can
   * see them. */
  onAccountsChanged: (accounts: AccountOut[]) => void;
  onReadyChange?: (state: { busy: boolean; canContinue: boolean; save: () => void }) => void;
}) {
  const { investorId, token } = useAuth();
  const [brokers, setBrokers] = useState<BrokerOut[] | null>(null);
  const [existing, setExisting] = useState<AccountOut[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!investorId) return;
    let cancelled = false;
    Promise.all([
      api.get<BrokerOut[]>("/brokers", { token }),
      api.get<AccountOut[]>(`/investors/${investorId}/accounts`, { token }),
    ])
      .then(([registry, accounts]) => {
        if (cancelled) return;
        setBrokers(registry);
        setExisting(accounts);
      })
      .catch((err) => {
        if (!cancelled) setError(friendly(err));
      });
    return () => {
      cancelled = true;
    };
  }, [investorId, token]);

  /** Broker codes this investor already has an account for. Compared case-insensitively:
   *  `account.broker` is free text and older rows were written by a form that did not
   *  normalise it. */
  const owned = useMemo(
    () => new Set(existing.map((a) => a.broker.trim().toUpperCase())),
    [existing]
  );

  const canContinue = picked.size > 0 || owned.size > 0;

  const save = useCallback(async () => {
    if (!investorId) return;
    const wanted = [...picked].filter((code) => !owned.has(code));
    if (wanted.length === 0) {
      // Everything ticked already exists. Nothing to write, and writing anyway would
      // leave a second account for the same broker that nothing could tell apart.
      onAccountsChanged(existing);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created: AccountOut[] = [];
      for (const broker of wanted) {
        created.push(
          await api.post<AccountOut>(`/investors/${investorId}/accounts`, {
            token,
            // client_id is left null on purpose. It is the broker's own code for this
            // account (Zerodha calls it UCC), and asking a person to go and look one up
            // before they have seen anything would be the worst possible first question.
            // It fills itself in from the first document that states it.
            json: { broker, dp_id: null, client_id: null, is_advised: true, label: null },
          })
        );
      }
      const all = [...existing, ...created];
      setExisting(all);
      setPicked(new Set());
      onAccountsChanged(all);
    } catch (err) {
      setError(friendly(err));
    } finally {
      setBusy(false);
    }
  }, [investorId, token, picked, owned, existing, onAccountsChanged]);

  useEffect(() => {
    onReadyChange?.({ busy, canContinue, save: () => void save() });
  }, [busy, canContinue, save, onReadyChange]);

  if (!investorId) return null;

  if (error && brokers === null) {
    return (
      <Banner tone="negative" title="We couldn't load the broker list">
        {error}
      </Banner>
    );
  }

  if (brokers === null) {
    return (
      <p style={{ margin: 0, font: "var(--type-body)", color: "var(--text-tertiary)" }}>
        Loading brokers…
      </p>
    );
  }

  const readableNames = brokers.filter((b) => b.documents.length > 0).map((b) => b.name);
  const comingSoon = brokers.filter((b) => b.documents.length === 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {error && (
        <Banner tone="negative" title="We couldn't add that">
          {error}
        </Banner>
      )}

      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
        {brokers.map((broker) => {
          const already = owned.has(broker.code);
          const tag = already ? "already added" : broker.documents.length > 0 ? null : "Coming soon";
          const name = (
            <span
              style={{
                flex: 1,
                minWidth: 140,
                font: "var(--type-body)",
                fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
                color: "var(--text-primary)",
              }}
            >
              {broker.name}
            </span>
          );
          return (
            <li
              key={broker.code}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                flexWrap: "wrap",
                padding: "10px 12px",
                border: "1px solid var(--border)",
                background: tag ? "var(--surface-sunken)" : "var(--surface)",
                opacity: tag ? 0.6 : 1,
              }}
            >
              {tag ? (
                name
              ) : (
                <Checkbox
                  id={`broker-${broker.code}`}
                  checked={picked.has(broker.code)}
                  onChange={(on) =>
                    setPicked((current) => {
                      const next = new Set(current);
                      if (on) next.add(broker.code);
                      else next.delete(broker.code);
                      return next;
                    })
                  }
                  label={name}
                  style={{ flex: 1, minWidth: 140 }}
                />
              )}
              {tag && (
                <span style={{ font: "var(--type-label)", color: "var(--text-tertiary)" }}>
                  {tag}
                </span>
              )}
            </li>
          );
        })}
      </ul>

      {readableNames.length > 0 && comingSoon.length > 0 && (
        <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          {listOf(readableNames)} {readableNames.length === 1 ? "is" : "are"} the only broker
          {readableNames.length === 1 ? "" : "s"} we can read files from right now. The others
          are coming soon. If you do not use {readableNames.length === 1 ? "it" : "them"}, skip
          this step and have a look around. You can add a broker later in Settings.
        </p>
      )}
    </div>
  );
}

/** "Zerodha", or "Zerodha and Groww", or "Zerodha, Groww and Upstox" — so the sentence
 *  under the list stays a sentence as parsers are added. */
function listOf(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function friendly(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return "Your session looks stale. Sign out and back in, then try again.";
    }
    if (err.status >= 500) return "Our server had a problem with that. Try again in a moment.";
    return err.message;
  }
  return "We couldn't reach the server. Check your connection and try again.";
}
