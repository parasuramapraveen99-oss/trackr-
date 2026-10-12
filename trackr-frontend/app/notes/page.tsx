"use client";

import { useMemo, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import { formatMoney, formatPercent, parseDecimal } from "@/lib/format";
import type { SummaryResponse } from "@/lib/api-types";
import { PageHeader } from "../components/PageHeader";
import { PageBody } from "../components/Shell";
import {
  Badge,
  Banner,
  BubbleMenu,
  Button,
  DonutChart,
  Icon,
  IconButton,
  SlashMenu,
  type Slice,
} from "../components/ds";

const NOTE_TITLES = ["Q2 review", "Sell discipline", "Reconciliation log", "Tax notes FY26"];

/**
 * The notes surface — a document with portfolio embeds.
 *
 * This screen is a **design proposal, not a shipped feature**: there is no notes table
 * behind it, so nothing typed here is saved. It is built out because the surface is
 * where the product's thesis lands — "tracking is table stakes; explanation is the
 * product" — and because the editor chrome (bubble menu, slash menu, embed blocks) has
 * to exist before a headless tiptap instance can be dropped into it.
 *
 * The one thing it does NOT fake is data: the embedded allocation donut reads the real
 * /summary response, so what it shows is true even though the prose around it is a
 * placeholder.
 *
 * It carries no AI that reads the note or the portfolio. Trackr+'s only assistant is "Ask
 * about markets", which answers public questions and cannot see portfolio data
 * (docs/AI_ASSISTANT_PRIVACY_PLAN.md section 1), so an "Ask AI about this" or "Summarise
 * performance" action here would promise something the product deliberately refuses.
 */
export default function NotesPage() {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const [slashOpen, setSlashOpen] = useState(false);
  const [activeNote, setActiveNote] = useState(0);

  const summary = useFetch<SummaryResponse>(
    investorId ? `/investors/${investorId}/summary` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined },
    token
  );

  // A sector with no known weight is LEFT OUT of the chart, not drawn as a 0% slice --
  // the ratio is multiplied in Decimal and becomes a number only as the slice's sweep.
  const sectorSlices: Slice[] = useMemo(
    () =>
      (summary.data?.allocation_by_sector ?? []).slice(0, 6).flatMap((g) => {
        const weight = parseDecimal(g.weight_pct);
        return weight === null ? [] : [{ label: g.label, value: weight.times(100).toNumber() }];
      }),
    [summary.data]
  );

  const s = summary.data;

  return (
    <>
      <PageHeader title="Notes" subtitle="Working notes, with your portfolio embedded." />

      <PageBody>
        <Banner tone="info" title="Preview: notes are not saved yet">
          The editor chrome is real; the storage behind it is not. Nothing typed on this page persists between visits.
          The embedded allocation below does read your live portfolio.
        </Banner>

        <div style={{ display: "grid", gridTemplateColumns: "200px minmax(0, 1fr)", gap: 32, maxWidth: 1040, marginTop: 24 }}>
          <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <p
              style={{
                margin: "0 0 8px",
                font: "var(--type-label)",
                textTransform: "uppercase",
                letterSpacing: "var(--tracking-label)",
                color: "var(--text-tertiary)",
              }}
            >
              Notes
            </p>
            {NOTE_TITLES.map((n, i) => (
              <button
                key={n}
                type="button"
                onClick={() => setActiveNote(i)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  textAlign: "left",
                  border: "none",
                  cursor: "pointer",
                  padding: "6px 8px",
                  borderRadius: "var(--radius-sm)",
                  font: "var(--type-body)",
                  background: i === activeNote ? "var(--surface-active)" : "transparent",
                  color: i === activeNote ? "var(--text-primary)" : "var(--text-secondary)",
                }}
              >
                <Icon name="document" size={15} />
                {n}
              </button>
            ))}
            <div style={{ marginTop: 10 }}>
              <Button variant="ghost" size="sm" icon="plus">
                New note
              </Button>
            </div>
          </nav>

          <div style={{ position: "relative" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <BubbleMenu
                active={["bold"]}
                items={[
                  { key: "h1", label: "Heading 1", text: "H1" },
                  { key: "h2", label: "Heading 2", text: "H2" },
                  { key: "d1", label: "", divider: true },
                  { key: "bold", label: "Bold", icon: "bold" },
                  { key: "italic", label: "Italic", icon: "italic" },
                  { key: "link", label: "Link", icon: "link" },
                ]}
              />
              <IconButton
                name="plus"
                label="Insert block"
                variant="plain"
                size={30}
                active={slashOpen}
                onClick={() => setSlashOpen((v) => !v)}
              />
              <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
                or type <code style={{ font: "var(--type-mono)" }}>/</code> to insert
              </span>
            </div>

            {slashOpen && (
              <div style={{ position: "absolute", top: 48, left: 0, zIndex: 30 }}>
                <SlashMenu
                  activeKey="donut"
                  onSelect={() => setSlashOpen(false)}
                  groups={[
                    {
                      label: "Basic",
                      items: [
                        { key: "text", label: "Text", icon: "text", description: "Plain paragraph" },
                        { key: "heading", label: "Heading", icon: "document", description: "Section title" },
                      ],
                    },
                    {
                      label: "Portfolio",
                      items: [
                        {
                          key: "donut",
                          label: "Allocation donut",
                          icon: "performance",
                          description: "Live split by sector",
                        },
                        { key: "holdings", label: "Holdings table", icon: "holdings", description: "Top positions, priced" },
                        { key: "kpi", label: "Metric tile", icon: "percent", description: "One figure, as of a date" },
                      ],
                    },
                  ]}
                />
              </div>
            )}

            <div className="tiptap" style={{ paddingBottom: 24 }}>
              <h1 style={{ fontSize: "var(--text-4xl)" }}>{NOTE_TITLES[activeNote]}</h1>
              <p style={{ color: "var(--text-tertiary)", font: "var(--type-body-sm)" }}>
                Draft · <Badge tone="outline">Not saved</Badge>
              </p>
              <p>
                Holdings are never stored. Every figure on this page is recomputed from the transaction ledger when it
                is read. That is what makes a note like this one checkable months later: the numbers it quotes can be
                reproduced by replaying the same rows.
              </p>
              <ul>
                <li>Unrealised P&amp;L stands at {formatMoney(s?.unrealised)} ({formatPercent(s?.unrealised_pct)}).</li>
                <li>Invested cost is {formatMoney(s?.invested)} against a market value of {formatMoney(s?.market_value)}.</li>
                <li>
                  {s?.unpriced.count
                    ? `${s.unpriced.count} holding(s) are unpriced and partitioned out of XIRR.`
                    : "Every holding is priced, so XIRR covers the whole book."}
                </li>
              </ul>
              <blockquote>Tracking is table stakes; explanation is the product.</blockquote>

              <div
                style={{
                  margin: "20px 0",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius-lg)",
                  padding: 16,
                  background: "var(--surface)",
                }}
              >
                <p
                  style={{
                    margin: "0 0 12px",
                    font: "var(--type-label)",
                    textTransform: "uppercase",
                    letterSpacing: "var(--tracking-label)",
                    color: "var(--text-tertiary)",
                  }}
                >
                  Embedded · allocation by sector
                </p>
                {sectorSlices.length === 0 ? (
                  <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
                    No sector allocation to embed yet.
                  </p>
                ) : (
                  <DonutChart
                    size={124}
                    slices={sectorSlices}
                    centerValue={formatMoney(s?.market_value)}
                    centerLabel="Current value"
                  />
                )}
              </div>

              <div
                style={{
                  margin: "20px 0",
                  border: "1px solid var(--info)",
                  background: "var(--info-tint)",
                  borderRadius: "var(--radius-lg)",
                  padding: 14,
                }}
              >
                <p
                  style={{
                    margin: "0 0 8px",
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    font: "var(--type-body-sm)",
                    fontWeight: 500,
                    color: "var(--info)",
                  }}
                >
                  <Icon name="sparkles" size={14} /> Ask about markets
                </p>
                <p style={{ margin: 0, font: "var(--type-body)", color: "var(--text-primary)" }}>
                  The assistant in the drawer answers public questions about markets, companies and financial
                  concepts. It cannot read this note or your portfolio.
                </p>
              </div>

              <p>Next: check whether drift in the largest sector is a decision or a consequence of price moves.</p>
            </div>
          </div>
        </div>
      </PageBody>
    </>
  );
}
