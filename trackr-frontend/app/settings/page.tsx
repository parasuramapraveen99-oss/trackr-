"use client";

import Link from "next/link";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { ClientsOverview, InvestorIn, InvestorOut } from "@/lib/api-types";
import { useFetch } from "@/lib/use-fetch";
import { AccountOnboarding } from "../components/AccountOnboarding";
import { ConsentCapture } from "../components/ConsentCapture";
import { FilePasswords } from "../components/FilePasswords";
import { ClientsTree } from "../components/ClientsTree";
import { PageHeader } from "../components/PageHeader";
import { PageBody } from "../components/Shell";
import { Banner, Button, Field, Input, Select } from "../components/ds";
import { EMPTY_VALUE } from "@/lib/format";

const EMPTY_INVESTOR: InvestorIn = { display_name: "", family_group_id: null, onboarding_date: null };

/** The rail, in the order someone actually needs them: who you are, which portfolio,
 *  what we may do with it, where files come from, and the way out. */
const TABS = [
  { key: "account", label: "Account", blurb: "The login itself." },
  { key: "portfolios", label: "Portfolios", blurb: "View your clients and add new ones." },
  { key: "permission", label: "Permission", blurb: "What you've allowed us to process." },
  { key: "brokers", label: "Brokers", blurb: "Where this portfolio's documents come from." },
  { key: "passwords", label: "File passwords", blurb: "Passwords that open your locked statements." },
  { key: "privacy", label: "Privacy & data", blurb: "Export or delete everything." },
] as const;

type TabKey = (typeof TABS)[number]["key"];

/**
 * Settings: the configuration screen, and no longer the first thing anyone sees.
 *
 * IT USED TO BE WHERE REGISTRATION LANDED, which was the single worst first impression in
 * the product — the most technical screen in the app, all investor codes and family group
 * IDs, shown to someone who had not yet seen a number. Registration now lands on the
 * dashboard (app/register/page.tsx). This screen is reached deliberately, by someone who
 * came here to change something, and is written for that person.
 *
 * NOW A RAIL, NOT A SCROLL. Five unrelated things stacked in one column meant Permission,
 * Broker accounts and "Add another portfolio" all had equal weight and you had to read
 * past the ones you did not want. Worse, "Add another portfolio" — a rare adviser action
 * asking for a Code and a Family group ID — sat at the same size as the broker setup
 * almost everyone comes here for. Grouping by what a person came to do puts one subject on
 * screen at a time, and the rail says what the others are without showing them.
 *
 * DEEP-LINKABLE BY HASH, because other screens send people here for a specific reason —
 * app/privacy/page.tsx says "Open Settings to pick a portfolio" and now lands on the tab
 * that does that, rather than at the top of a page where it is the fourth thing down.
 */
export default function SettingsPage() {
  const { user, investorId, setInvestorId, token, asOf } = useAuth();
  // One request feeds both the clients tree and the family menu in "Add another portfolio".
  const overview = useFetch<ClientsOverview>(
    token ? "/investors/overview" : null,
    { as_of: asOf },
    token
  );
  const [tab, setTab] = useState<TabKey>("account");
  const [newInvestor, setNewInvestor] = useState<InvestorIn>(EMPTY_INVESTOR);
  const [creatingInvestor, setCreatingInvestor] = useState(false);
  const [investorError, setInvestorError] = useState<string | null>(null);
  const [investorCreated, setInvestorCreated] = useState(false);

  // Header links can target another tab while Settings is already mounted.
  useEffect(() => {
    const readHash = () => {
      const hash = window.location.hash.replace("#", "");
      if (TABS.some((t) => t.key === hash)) setTab(hash as TabKey);
    };
    readHash();
    window.addEventListener("hashchange", readHash);
    return () => window.removeEventListener("hashchange", readHash);
  }, []);

  function selectTab(key: TabKey) {
    setTab(key);
    // replaceState, not a router push: switching tabs is not a navigation anyone wants to
    // walk back through with the Back button.
    window.history.replaceState(null, "", `#${key}`);
  }

  function setInvestorField<K extends keyof InvestorIn>(key: K, value: InvestorIn[K]) {
    setNewInvestor((f) => ({ ...f, [key]: value }));
  }

  async function handleCreateInvestor(e: React.FormEvent) {
    e.preventDefault();
    setCreatingInvestor(true);
    setInvestorError(null);
    try {
      const investor = await api.post<InvestorOut>("/investors", {
        json: {
          ...newInvestor,
          display_name: newInvestor.display_name.trim(),
          family_group_id: newInvestor.family_group_id || null,
        },
        token,
      });
      setInvestorId(investor.id);
      overview.refetch();
      setNewInvestor(EMPTY_INVESTOR);
      setInvestorCreated(true);
      setTimeout(() => setInvestorCreated(false), 2000);
    } catch (err) {
      setInvestorError(err instanceof ApiError ? err.message : "Could not create investor");
    } finally {
      setCreatingInvestor(false);
    }
  }

  const active = TABS.find((t) => t.key === tab) ?? TABS[0];

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Your login, the portfolio you're looking at, and what we're allowed to do with it."
      />
      <PageBody column>
        <div
          style={{
            display: "grid",
            // The rail collapses above the content on a narrow screen rather than
            // squeezing into a column too thin to read.
            gridTemplateColumns: "minmax(0, 1fr)",
            gap: 20,
            alignItems: "start",
          }}
          className="settings-layout"
        >
          <nav aria-label="Settings sections" style={{ display: "grid", gap: 2, alignContent: "start" }}>
            {TABS.map((t) => {
              const isActive = t.key === tab;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => selectTab(t.key)}
                  aria-current={isActive ? "page" : undefined}
                  style={{
                    textAlign: "left",
                    cursor: "pointer",
                    padding: "9px 12px",
                    background: isActive ? "var(--surface-hover)" : "transparent",
                    border: "1px solid",
                    borderColor: isActive ? "var(--border)" : "transparent",
                    borderLeft: `2px solid ${isActive ? "var(--ledger-ink)" : "transparent"}`,
                    font: "var(--type-body)",
                    color: isActive ? "var(--text-primary)" : "var(--text-secondary)",
                    fontWeight: (isActive ? "var(--weight-medium)" : "inherit") as CSSProperties["fontWeight"],
                  }}
                >
                  {t.label}
                </button>
              );
            })}
          </nav>

          <div style={{ display: "grid", gap: 20, minWidth: 0 }}>
            {tab === "account" && (
              <Section title="Your login" blurb={active.blurb}>
                <dl style={definitionListStyle}>
                  <dt style={dtStyle}>Email</dt>
                  <dd style={ddStyle}>{user?.email}</dd>
                  <dt style={dtStyle}>Name</dt>
                  <dd style={ddStyle}>{user?.display_name ?? EMPTY_VALUE}</dd>
                </dl>
              </Section>
            )}

            {tab === "portfolios" && (
              <>
                <Section
                  title="Add a client"
                  blurb="Their portfolio opens as soon as you add them."
                >
                  <form onSubmit={handleCreateInvestor} style={{ display: "grid", gap: 12 }}>
                    {investorError && <Banner tone="negative">{investorError}</Banner>}
                    <div
                      style={{
                        display: "grid",
                        gap: 12,
                        gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                      }}
                    >
                      <Field label="Name" htmlFor="investor-name">
                        <Input
                          id="investor-name"
                          required
                          value={newInvestor.display_name}
                          onChange={(v) => setInvestorField("display_name", v)}
                          placeholder="Full name"
                        />
                      </Field>
                      <Field label="Family" hint="Optional." htmlFor="investor-family">
                        <Select
                          id="investor-family"
                          value={newInvestor.family_group_id ?? ""}
                          onChange={(v) => setInvestorField("family_group_id", v || null)}
                          options={[
                            { value: "", label: "No family" },
                            ...(overview.data?.families ?? []).map((f) => ({ value: f.id, label: f.name })),
                          ]}
                        />
                      </Field>
                      <Field
                        label="Start date"
                        hint="Optional. When you started advising them."
                        htmlFor="investor-date"
                      >
                        <Input
                          id="investor-date"
                          type="date"
                          value={newInvestor.onboarding_date ?? ""}
                          onChange={(v) => setInvestorField("onboarding_date", v || null)}
                        />
                      </Field>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                      <Button type="submit" disabled={creatingInvestor}>
                        {creatingInvestor ? "Adding…" : "Add client"}
                      </Button>
                      {investorCreated && (
                        <span style={{ font: "var(--type-body-sm)", color: "var(--positive)" }}>
                          Added. You're now viewing this client.
                        </span>
                      )}
                    </div>
                  </form>
                </Section>

                <Section title="Clients" blurb="Choose a client to view their portfolio.">
                  <div style={{ marginTop: 16 }}>
                    <ClientsTree overview={overview} />
                  </div>
                </Section>
              </>
            )}

            {tab === "permission" && (
              <Section
                title="Permission"
                blurb="We need your permission before we can add any data to this portfolio."
              >
                <ConsentCapture investorId={investorId} token={token} />
              </Section>
            )}

            {tab === "brokers" && (
              <Section
                title="Broker accounts"
                blurb="These accounts identify which broker your imports belong to. Add an account when you start using another broker."
              >
                <AccountOnboarding investorId={investorId} token={token} />
              </Section>
            )}

            {tab === "passwords" && (
              <Section
                title="File passwords"
                blurb="Your PAN and saved passwords help open locked statements during import. Stored values are encrypted."
              >
                <FilePasswords investorId={investorId} token={token} />
              </Section>
            )}

            {tab === "privacy" && (
              <Section
                title="Privacy & your data"
                blurb="Export everything held about this portfolio, or permanently delete it, under your rights in the DPDP Act, 2023."
              >
                <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
                  <Link href="/privacy">Go to Privacy &amp; data →</Link>
                  {"  ·  "}
                  <Link href="/terms">Terms &amp; Privacy Policy</Link>
                </p>
              </Section>
            )}
          </div>
        </div>
      </PageBody>

      {/* The rail only becomes a rail where there is room for one. Inline rather than in
          globals.css because it is the one layout rule this screen owns. */}
      <style>{`
        @media (min-width: 720px) {
          .settings-layout { grid-template-columns: 180px minmax(0, 1fr) !important; gap: 28px !important; }
        }
      `}</style>
    </>
  );
}

function Section({ title, blurb, children }: { title: string; blurb?: string; children: ReactNode }) {
  return (
    <section
      style={{
        border: "1px solid var(--border)",
        background: "var(--surface)",
        boxShadow: "var(--shadow-card)",
        padding: 18,
      }}
    >
      <h2
        style={{
          margin: 0,
          font: "var(--type-h2)",
          color: "var(--text-primary)",
        }}
      >
        {title}
      </h2>
      {blurb && (
        <p
          style={{
            margin: "4px 0 0",
            font: "var(--type-body-sm)",
            color: "var(--text-tertiary)",
            maxWidth: "62ch",
          }}
        >
          {blurb}
        </p>
      )}
      <div style={{ marginTop: 14 }}>{children}</div>
    </section>
  );
}

const definitionListStyle: CSSProperties = {
  margin: 0,
  display: "grid",
  gridTemplateColumns: "120px 1fr",
  gap: "6px 12px",
  font: "var(--type-body-sm)",
};

const dtStyle: CSSProperties = { color: "var(--text-tertiary)" };
const ddStyle: CSSProperties = { margin: 0, color: "var(--text-primary)" };
