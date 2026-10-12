"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import { broadcastPortfolioRefresh } from "@/lib/onboarding";
import { api, useReferenceSync, type ReferenceSyncState } from "@trackr/core";
import { Banner, Button, Card } from "./ds";

interface UnresolvedItem {
  isin: string;
  reason: string;
  ex_date?: string | null;
  /** Company and shares held the day before the ex-date (only asked when above zero). */
  name?: string | null;
  symbol?: string | null;
  held?: string | null;
}

interface ReferenceStatus {
  enabled: boolean;
  state: ReferenceSyncState;
  pending: number;
  retrying: number;
  last_success_at: string | null;
  unresolved: UnresolvedItem[];
}

type Answer = "NOT_PARTICIPATED" | "PARTICIPATED" | "UNSURE";

/** The notice's plain name. The raw subject is the exchange's wording, not the user's. */
export function noticeName(subject: string): string {
  if (/buy\s*-?\s*back/i.test(subject)) return "Buyback";
  if (/rights/i.test(subject)) return "Rights issue";
  if (/bonus/i.test(subject)) return "Bonus issue";
  if (/split/i.test(subject)) return "Share split";
  if (/dividend/i.test(subject)) return "Dividend";
  return "Corporate action";
}

/** "04-Sep-2026" -> "4 Sep 2026". Anything else is shown as received. */
export function noticeDate(exDate: string): string {
  const match = /^(\d{2})-([A-Za-z]{3})-(\d{4})$/.exec(exDate);
  if (!match) return exDate;
  return `${Number(match[1])} ${match[2]} ${match[3]}`;
}

/** "32.000000" -> "32"; "1234.5" -> "1,234.5". */
function shares(value: string): string {
  return Number(value).toLocaleString("en-IN", { maximumFractionDigits: 4 });
}

/** "PVR INOX LIMITED" -> "PVR INOX". The suffix adds length, not meaning. */
function companyName(item: UnresolvedItem): string {
  const name = (item.name ?? "").replace(/\s+(LIMITED|LTD\.?)$/i, "").trim();
  return name || item.symbol || item.isin;
}

/** One short question per kind of event, answerable from a CAS or contract note. */
function wording(kind: string, company: string, held: string) {
  if (kind === "Buyback") {
    return {
      ask: `You had ${held} shares. Did you sell any in this buyback?`,
      yes: "Yes, I sold some",
      next: "Upload that contract note below.",
    };
  }
  if (kind === "Rights issue") {
    return {
      ask: `You had ${held} shares. Did you buy new shares in this rights issue?`,
      yes: "Yes, I bought some",
      next: "Upload your allotment or contract note below.",
    };
  }
  return {
    ask: `You had ${held} shares. Did this change them?`,
    yes: "Yes",
    next: "Upload the document that shows the change below.",
  };
}

const TEXT = { fontSize: "var(--text-sm)", lineHeight: 1.55, color: "var(--text-secondary)" } as const;

function NoticeRow({
  item,
  investorId,
  token,
  onAnswered,
}: {
  item: UnresolvedItem;
  investorId: string;
  token: string | null;
  onAnswered: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [chosen, setChosen] = useState<Answer | null>(null);
  const [failed, setFailed] = useState(false);

  // Notices that only say "being checked" have no date to answer against.
  if (!item.ex_date) return null;

  async function answer(outcome: Answer) {
    setSaving(true);
    setFailed(false);
    try {
      await api.post(`/investors/${investorId}/reference-notices/outcome`, {
        json: { isin: item.isin, ex_date: item.ex_date, subject: item.reason, outcome },
        token,
      });
      setChosen(outcome);
      onAnswered();
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  }

  const kind = noticeName(item.reason);
  const company = companyName(item);
  const held = item.held ? shares(item.held) : "some";
  const w = wording(kind, company, held);

  return (
    <li style={{ display: "flex", flexDirection: "column", gap: 8, padding: "14px 0", borderTop: "1px solid var(--border)" }}>
      <div style={{ fontSize: "var(--text-base)", fontWeight: 600, color: "var(--text-primary)" }}>
        {company} · {kind} · {noticeDate(item.ex_date)}
      </div>
      <p style={TEXT}>{w.ask}</p>
      <p style={{ ...TEXT, color: "var(--text-tertiary)" }}>Check your CAS or contract notes.</p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 4 }}>
        <Button variant="secondary" disabled={saving} onClick={() => answer("NOT_PARTICIPATED")}>
          No
        </Button>
        <Button variant="secondary" disabled={saving} onClick={() => answer("PARTICIPATED")}>
          {w.yes}
        </Button>
        <Button variant="ghost" disabled={saving} onClick={() => answer("UNSURE")}>
          Check later
        </Button>
      </div>
      {chosen === "PARTICIPATED" && <p style={{ ...TEXT, color: "var(--text-primary)" }}>{w.next}</p>}
      {chosen === "UNSURE" && <p style={TEXT}>We&apos;ll keep this here.</p>}
      {failed && <p style={{ ...TEXT, color: "var(--negative)" }}>Could not save. Try again.</p>}
    </li>
  );
}

/** How many notices still need the investor's answer. Drives the dot on the Import icon. */
export function useNoticesNeeded(): number {
  const { investorId, token } = useAuth();
  const result = useFetch<ReferenceStatus>(
    investorId ? `/investors/${investorId}/reference-status` : null,
    undefined, token, { refreshMs: 60_000 },
  );
  return (result.data?.unresolved ?? []).filter((item) => !!item.ex_date).length;
}

export function ReferenceSyncBanner() {
  const { investorId, token } = useAuth();
  const result = useFetch<ReferenceStatus>(
    investorId ? `/investors/${investorId}/reference-status` : null,
    undefined, token, { refreshMs: 15_000 },
  );
  // Drive the queue while it has work: production has no always-on worker, so without
  // this a fresh import waits for the daily cron. See @trackr/core use-reference-sync.
  useReferenceSync(investorId, token, result.data?.state, result.refetch);
  const previous = useRef<string | null>(null);
  const stamp = result.data?.last_success_at ?? null;
  useEffect(() => {
    const key = `${investorId}:${stamp}`;
    if (previous.current !== null && previous.current !== key) broadcastPortfolioRefresh();
    previous.current = key;
  }, [investorId, stamp]);
  // Only the questions that need the investor's answer are shown. Status wording
  // (updating, retrying, off) is internal and stays out of the product.
  const data = result.data;
  const questions = (data?.unresolved ?? []).filter((item) => !!item.ex_date);
  if (!investorId) return null;
  if (result.error) {
    // These questions decide whether a rights issue or buyback belongs in the ledger.
    // Hiding them on a failed read would make "nothing to answer" look true.
    return (
      <Banner tone="negative" title="Could not check for questions about your holdings" style={{ marginBottom: "var(--space-6)" }}>
        {result.error}
      </Banner>
    );
  }
  if (questions.length === 0) return null;
  const n = questions.length;
  return (
    <div style={{ marginBottom: "var(--space-6)" }}>
      <Card
        title={`${n} quick check${n > 1 ? "s" : ""}`}
      >
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {questions.map((item, index) => (
            <NoticeRow
              key={`${item.isin}:${item.ex_date ?? ""}:${index}`}
              item={item}
              investorId={investorId}
              token={token}
              onAnswered={result.refetch}
            />
          ))}
        </ul>
      </Card>
    </div>
  );
}
