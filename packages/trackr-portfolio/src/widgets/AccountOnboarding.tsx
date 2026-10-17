"use client";

import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { api, ApiError, useFetch } from "@trackr/core";
import type { AccountIn, AccountOut, BrokerOut } from "@trackr/core";
import { Badge, Banner, Button, Field, Input, Select, Skeleton } from "@trackr/ui";

const EMPTY: AccountIn = { broker: "", dp_id: null, client_id: null, is_advised: true, label: null };

/**
 * Broker accounts, in Settings: the long-form version of the wizard's broker picker.
 *
 * TWO THINGS CHANGED. The broker was a free-text box that accepted anything — so `ZERODA`
 * created an account nothing could parse and nothing warned about — and is now a picker
 * fed by `GET /brokers`, the same registry the wizard uses. Free text is still reachable
 * under "Someone else", because a broker we have not listed is a real thing a person may
 * have and refusing them an account would be worse than a row with no adapter.
 *
 * And the fields are ordered by who needs them. Broker and label are what anyone adding a
 * second account fills in; DP ID and client ID are identifiers most people would have to
 * go and look up, and the client ID fills itself in from the first document that states
 * it, so both sit behind a disclosure rather than in the way.
 *
 * Restyled onto the design-system tokens along with the rest of Settings — it was one of
 * the last screens still written in the retired Tailwind class names, which resolved to
 * roughly the right colours and the wrong shapes.
 */
export function AccountOnboarding({
  investorId,
  token,
  onAdded,
}: {
  investorId: string;
  token: string | null;
  /** Fires after an account is created -- e.g. so Import can move straight on to upload. */
  onAdded?: (account: AccountOut) => void;
}) {
  // The server is the source of truth for which accounts exist. This used to be a
  // localStorage cache of POST responses, back when the contract had no GET -- but
  // GET /investors/{id}/accounts exists now, and a browser-only cache meant a new
  // browser (or a cleared one) showed "No accounts added yet" for an investor whose
  // account was right there in the database.
  const accounts = useFetch<AccountOut[]>(
    investorId ? `/investors/${investorId}/accounts` : null,
    undefined,
    token
  );
  const [brokers, setBrokers] = useState<BrokerOut[]>([]);
  const [form, setForm] = useState<AccountIn>(EMPTY);
  const [otherBroker, setOtherBroker] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<BrokerOut[]>("/brokers", { token })
      .then((registry) => {
        if (!cancelled) setBrokers(registry);
      })
      .catch(() => {
        // Without the registry the "Someone else" free-text path still works, which is
        // the honest degradation: fewer choices offered, nothing broken.
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  function set<K extends keyof AccountIn>(key: K, value: AccountIn[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  const chosenBroker = form.broker === "__other" ? otherBroker.trim().toUpperCase() : form.broker;

  const options = useMemo(
    () => [
      { value: "", label: "Choose a broker…" },
      ...brokers.map((b) => ({ value: b.code, label: b.name })),
      { value: "__other", label: "Someone else…" },
    ],
    [brokers]
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!investorId || !chosenBroker) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.post<AccountOut>(`/investors/${investorId}/accounts`, {
        json: { ...form, broker: chosenBroker },
        token,
      });
      setForm(EMPTY);
      setOtherBroker("");
      accounts.refetch();
      onAdded?.(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add account");
    } finally {
      setSubmitting(false);
    }
  }

  if (!investorId) {
    return (
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        Create an investor above before adding a broker account.
      </p>
    );
  }

  const accountList = accounts.data ?? [];

  return (
    <div style={{ display: "grid", gap: 20 }}>
      <details open={accountList.length === 0}>
        <summary style={{ cursor: "pointer", font: "var(--type-body-sm)", color: "var(--text-primary)", padding: "4px 0" }}>
          {accountList.length === 0 ? "Add a broker account" : "Add another account"}
        </summary>
        <form onSubmit={handleSubmit} style={{ display: "grid", gap: 12, marginTop: 12 }}>
          {error && <Banner tone="negative">{error}</Banner>}

          <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
            <Field label="Broker" htmlFor="account-broker">
              <Select
                id="account-broker"
                value={form.broker}
                onChange={(v) => set("broker", v)}
                options={options}
                style={{ width: "100%" }}
              />
            </Field>
            {form.broker === "__other" && (
              <Field label="Broker name" htmlFor="account-broker-other">
                <Input
                  id="account-broker-other"
                  value={otherBroker}
                  onChange={setOtherBroker}
                  placeholder="NAME IT"
                  style={{ textTransform: "uppercase" }}
                />
              </Field>
            )}
            <Field label="Label" hint="Optional. A name for this account." htmlFor="account-label">
              <Input
                id="account-label"
                value={form.label ?? ""}
                onChange={(v) => set("label", v || null)}
                placeholder="Primary Zerodha"
              />
            </Field>
          </div>

          <details>
            <summary style={{ cursor: "pointer", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
              DP ID and Client ID (optional)
            </summary>
            <div
              style={{
                display: "grid",
                gap: 12,
                gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                marginTop: 10,
              }}
            >
              <Field label="DP ID" htmlFor="account-dp">
                <Input
                  id="account-dp"
                  value={form.dp_id ?? ""}
                  onChange={(v) => set("dp_id", v || null)}
                  style={{ fontFamily: "var(--font-mono)" }}
                />
              </Field>
              <Field
                label="Client ID"
                hint="Your broker's own code for this account. Fills itself in from the first document that states it."
                htmlFor="account-client"
              >
                <Input
                  id="account-client"
                  value={form.client_id ?? ""}
                  onChange={(v) => set("client_id", v || null)}
                  style={{ fontFamily: "var(--font-mono)" }}
                />
              </Field>
            </div>
          </details>

          <div>
            <Button type="submit" disabled={submitting || !chosenBroker}>
              {submitting ? "Adding…" : "Add account"}
            </Button>
          </div>
        </form>
      </details>

      {accounts.loading ? (
        // Two account-card-shaped placeholders, for the same reason the consent panel has
        // one: "Loading accounts…" is one short line where the answer is several tall rows,
        // so the card grew under the cursor the moment the list arrived.
        <div style={{ display: "grid", gap: 12 }}>
          <Skeleton height={72} />
          <Skeleton height={72} />
        </div>
      ) : accounts.error ? (
        <Banner tone="negative">{accounts.error}</Banner>
      ) : accountList.length === 0 ? (
        <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          No accounts yet. Add one above and it becomes a place for files to land.
        </p>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {accountList.map((account) => (
            <AccountCard key={account.id} account={account} brokers={brokers} />
          ))}
        </div>
      )}
    </div>
  );
}

function AccountCard({ account, brokers }: { account: AccountOut; brokers: BrokerOut[] }) {
  const known = brokers.find((b) => b.code === account.broker.trim().toUpperCase());
  return (
    <div style={{ border: "1px solid var(--border)", background: "var(--surface)", padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <p
          style={{
            margin: 0,
            font: "var(--type-body)",
            fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
            color: "var(--text-primary)",
          }}
        >
          {account.label ?? known?.name ?? account.broker}
        </p>
        <Badge tone="neutral" mono>
          {account.broker}
        </Badge>
      </div>
    </div>
  );
}
