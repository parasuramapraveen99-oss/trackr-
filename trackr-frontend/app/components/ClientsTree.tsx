"use client";

import { useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { ClientsOverview, FamilyOut, OverviewClient, OverviewFigures } from "@/lib/api-types";
import type { useFetch } from "@/lib/use-fetch";
import { Dec, formatMoney, formatPercent, parseDecimal } from "@trackr/core";
import { Banner, Button, Input, SegmentedControl, Select } from "./ds";

/** What `useFetch<ClientsOverview>` returns. The Settings page owns the request so the
 * "Add a client" form can offer the same families. */
export type OverviewState = ReturnType<typeof useFetch<ClientsOverview>>;

type SortKey = "value" | "name" | "gain";

/** Summed figures for a family or for everyone. Only rows that HAVE figures are summed;
 * `excluded` counts the ones that could not be valued, so a total never claims them. */
interface Rollup {
  invested: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: string | null;
  excluded: number;
}

function rollUp(rows: OverviewFigures[]): Rollup {
  let invested = new Dec(0);
  let market = new Dec(0);
  let counted = 0;
  let excluded = 0;
  for (const row of rows) {
    const inv = parseDecimal(row.invested);
    const mv = parseDecimal(row.market_value);
    if (row.error || inv === null || mv === null) {
      excluded += 1;
      continue;
    }
    invested = invested.plus(inv);
    market = market.plus(mv);
    counted += 1;
  }
  if (counted === 0) {
    return { invested: null, market_value: null, unrealised: null, unrealised_pct: null, excluded };
  }
  const unrealised = market.minus(invested);
  return {
    invested: invested.toString(),
    market_value: market.toString(),
    unrealised: unrealised.toString(),
    unrealised_pct: invested.isZero() ? null : unrealised.div(invested).toString(),
    excluded,
  };
}

function compareBy(sort: SortKey) {
  return (a: { name: string; figures: Rollup | OverviewFigures }, b: typeof a): number => {
    if (sort === "name") return a.name.localeCompare(b.name);
    const key = sort === "value" ? "market_value" : "unrealised_pct";
    const av = parseDecimal(a.figures[key]);
    const bv = parseDecimal(b.figures[key]);
    if (av === null && bv === null) return a.name.localeCompare(b.name);
    if (av === null) return 1; // no figure goes last, never ranked as zero
    if (bv === null) return -1;
    return bv.comparedTo(av);
  };
}

/** "ZERODHA" -> "Zerodha". Broker names are stored upper-case. */
function brokerName(raw: string): string {
  return raw
    .toLowerCase()
    .split(/[\s_]+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Stocks a total leaves out, in one short sentence. */
function leftOut(row: OverviewFigures): string | null {
  const parts: string[] = [];
  if (row.unpriced_count) parts.push(`${plural(row.unpriced_count, "stock")} with no price yet`);
  if (row.skipped_count) parts.push(`${plural(row.skipped_count, "stock")} with missing trades`);
  return parts.length ? `Not counted: ${parts.join(", ")}.` : null;
}

// --- styles ------------------------------------------------------------------------------

const label: CSSProperties = {
  fontFamily: "var(--font-label)",
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text-tertiary)",
};

const small: CSSProperties = { font: "var(--type-body-sm)", color: "var(--text-tertiary)" };
const errorText: CSSProperties = { font: "var(--type-body-sm)", color: "var(--negative)" };

function tone(value: string | null): string {
  const v = parseDecimal(value);
  if (v === null || v.isZero()) return "var(--text-secondary)";
  return v.isNegative() ? "var(--negative)" : "var(--positive)";
}

function signedPercent(value: string | null): string {
  const v = parseDecimal(value);
  if (v === null) return "—";
  const text = formatPercent(value);
  return v.isNegative() || v.isZero() ? text : `+${text}`;
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform 0.15s ease" }}
    >
      <path d="M7.5 4 13 10l-5.5 6" />
    </svg>
  );
}

/** Value over gain, right-aligned: the two numbers a row is read for. */
function Amount({ figures, title }: { figures: Rollup | OverviewFigures; title?: string }) {
  return (
    <div style={{ textAlign: "right", minWidth: 120 }} title={title}>
      <div style={{ fontFamily: "var(--font-figure)", fontSize: 17, fontVariantNumeric: "tabular-nums", color: "var(--text-primary)" }}>
        {formatMoney(figures.market_value)}
      </div>
      <div style={{ font: "var(--type-body-sm)", fontVariantNumeric: "tabular-nums", color: tone(figures.unrealised_pct) }}>
        {signedPercent(figures.unrealised_pct)}
      </div>
    </div>
  );
}

function Row({
  depth,
  onToggle,
  expanded,
  children,
  right,
}: {
  depth: number;
  onToggle?: () => void;
  expanded?: boolean;
  children: ReactNode;
  right: ReactNode;
}) {
  return (
    <div
      onClick={onToggle}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: `12px 0 12px ${depth * 24}px`,
        borderTop: "1px solid var(--border)",
        cursor: onToggle ? "pointer" : "default",
      }}
    >
      <span style={{ width: 14, flexShrink: 0, color: "var(--text-tertiary)", display: "flex" }}>
        {onToggle && <Chevron open={!!expanded} />}
      </span>
      <div style={{ flex: 1, minWidth: 0, display: "grid", gap: 2 }}>{children}</div>
      {right}
    </div>
  );
}

// --- component ---------------------------------------------------------------------------

export function ClientsTree({ overview }: { overview: OverviewState }) {
  const { investorId, setInvestorId, token } = useAuth();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("value");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [addingFamily, setAddingFamily] = useState(false);
  const [newFamily, setNewFamily] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const data = overview.data;

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    try {
      await action();
      overview.refetch();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "That didn't save. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const tree = useMemo(() => {
    if (!data) return null;
    const q = query.trim().toLowerCase();
    const familyName = new Map(data.families.map((f) => [f.id, f.name]));
    const matches = (c: OverviewClient) =>
      !q ||
      c.display_name.toLowerCase().includes(q) ||
      c.code.toLowerCase().includes(q) ||
      (c.family_group_id !== null && (familyName.get(c.family_group_id) ?? "").toLowerCase().includes(q)) ||
      c.accounts.some((a) => `${a.broker} ${a.label ?? ""}`.toLowerCase().includes(q));
    const clients = data.clients.filter(matches);
    const order = compareBy(sort);
    const asNode = (c: OverviewClient) => ({ name: c.display_name || c.code, figures: c, client: c });

    const families = data.families
      .map((f: FamilyOut) => {
        const members = clients.filter((c) => c.family_group_id === f.id).map(asNode).sort(order);
        return { name: f.name, family: f, members, figures: rollUp(members.map((m) => m.client)) };
      })
      .filter((f) => f.members.length > 0 || !q)
      .sort(order);
    const loose = clients.filter((c) => c.family_group_id === null).map(asNode).sort(order);
    return { families, loose, total: rollUp(clients), shown: clients.length, all: data.clients.length };
  }, [data, query, sort]);

  if (overview.error) {
    return (
      <Banner tone="negative" title="Couldn't load your clients">
        {overview.error}
      </Banner>
    );
  }
  if (!data || !tree) return <p style={small}>Loading…</p>;

  const familyOptions = [
    { value: "", label: "No family" },
    ...data.families.map((f) => ({ value: f.id, label: f.name })),
  ];
  const searching = query.trim().length > 0;
  const isOpen = (id: string) => searching || open[id] === true;
  const toggle = (id: string) => setOpen((o) => ({ ...o, [id]: !o[id] }));
  const stop = (e: { stopPropagation: () => void }) => e.stopPropagation();

  function clientRows(node: { name: string; client: OverviewClient }, depth: number): ReactNode {
    const c = node.client;
    const active = c.id === investorId;
    const expanded = isOpen(c.id);
    const note = leftOut(c);
    const brokers = c.accounts.map((a) => brokerName(a.broker)).join(", ");
    return (
      <div key={c.id}>
        <Row
          depth={depth}
          expanded={expanded}
          onToggle={() => toggle(c.id)}
          right={
            <>
              <Amount figures={c} />
              <div style={{ width: 84, display: "flex", justifyContent: "flex-end" }} onClick={stop}>
                {active ? (
                  <span style={{ ...small, color: "var(--positive)" }}>Viewing</span>
                ) : (
                  <Button size="sm" variant="secondary" onClick={() => setInvestorId(c.id)}>
                    View
                  </Button>
                )}
              </div>
            </>
          }
        >
          <span style={{ font: "var(--type-body)", fontWeight: 500, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {node.name}
          </span>
          <span style={small}>
            <span style={{ fontFamily: "var(--font-mono)" }}>{c.code}</span>
            {brokers && ` · ${brokers}`}
          </span>
          {c.error && <span style={errorText}>Couldn&apos;t value this client: {c.error}</span>}
          {note && <span style={small}>{note}</span>}
        </Row>

        {expanded && (
          <>
            {c.accounts.map((a) => (
              <Row
                key={a.id}
                depth={depth + 1}
                right={
                  <>
                    <Amount figures={a} title="Based on this account's own trades." />
                    <div style={{ width: 84 }} />
                  </>
                }
              >
                <span style={{ font: "var(--type-body)" }}>
                  {brokerName(a.broker)}
                  {a.label && <span style={small}> · {a.label}</span>}
                  {a.is_advised && <span style={{ ...small, color: "var(--accent, var(--text-secondary))" }}> · Advised</span>}
                </span>
                {a.error && <span style={errorText}>Couldn&apos;t value this account: {a.error}</span>}
                {leftOut(a) && <span style={small}>{leftOut(a)}</span>}
              </Row>
            ))}
            <div
              style={{ display: "flex", alignItems: "center", gap: 10, padding: `8px 0 12px ${(depth + 1) * 24 + 26}px` }}
            >
              <span style={small}>Family</span>
              <Select
                size="sm"
                value={c.family_group_id ?? ""}
                options={familyOptions}
                onChange={(v) =>
                  run(() => api.put(`/investors/${c.id}/family`, { json: { family_id: v || null }, token }))
                }
                style={{ maxWidth: 180 }}
              />
            </div>
          </>
        )}
      </div>
    );
  }

  const kpis: [string, string, string][] = [
    ["Value", formatMoney(tree.total.market_value), "var(--text-primary)"],
    ["Invested", formatMoney(tree.total.invested), "var(--text-primary)"],
    [
      "Gain",
      `${formatMoney(tree.total.unrealised)}  ${signedPercent(tree.total.unrealised_pct)}`,
      tone(tree.total.unrealised),
    ],
  ];

  return (
    <div style={{ display: "grid", gap: 20 }}>
      {actionError && (
        <Banner tone="negative" title="Not saved">
          {actionError}
        </Banner>
      )}

      {/* Totals */}
      <div style={{ display: "grid", gap: 8 }}>
        <div style={{ display: "flex", gap: 36, flexWrap: "wrap" }}>
          {kpis.map(([name, value, color]) => (
            <div key={name} style={{ display: "grid", gap: 4 }}>
              <span style={label}>{name}</span>
              <span style={{ fontFamily: "var(--font-figure)", fontSize: 26, lineHeight: 1.1, fontVariantNumeric: "tabular-nums", color, whiteSpace: "pre" }}>
                {value}
              </span>
            </div>
          ))}
        </div>
        {(tree.all > 1 || tree.total.excluded > 0 || searching) && (
          <span style={small}>
            {searching ? `${plural(tree.shown, "client")} match` : `All ${plural(tree.all, "client")}`}
            {tree.total.excluded > 0 && ` · ${tree.total.excluded} not counted (couldn't be valued)`}
          </span>
        )}
      </div>

      {/* Tools */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ flex: "1 1 200px" }}>
          <Input icon="search" placeholder="Search" value={query} onChange={setQuery} />
        </div>
        <SegmentedControl<SortKey>
          size="sm"
          value={sort}
          onChange={setSort}
          options={[
            { value: "value", label: "Value" },
            { value: "name", label: "Name" },
            { value: "gain", label: "Gain" },
          ]}
        />
        <Button size="sm" variant="ghost" icon="plus" onClick={() => setAddingFamily((v) => !v)}>
          Family
        </Button>
      </div>

      {addingFamily && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const name = newFamily.trim();
            if (!name) return;
            void run(async () => {
              await api.post("/families", { json: { name }, token });
              setNewFamily("");
              setAddingFamily(false);
            });
          }}
          style={{ display: "flex", gap: 8, flexWrap: "wrap" }}
        >
          <div style={{ flex: "1 1 220px" }}>
            <Input placeholder="Family name, e.g. Sharma" value={newFamily} onChange={setNewFamily} />
          </div>
          <Button type="submit" size="sm" disabled={busy || !newFamily.trim()}>
            Add
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setAddingFamily(false)}>
            Cancel
          </Button>
        </form>
      )}

      {/* List */}
      <div style={{ borderBottom: "1px solid var(--border)" }}>
        {tree.families.length === 0 && tree.loose.length === 0 && (
          <p style={{ ...small, padding: "14px 0", margin: 0, borderTop: "1px solid var(--border)" }}>
            {searching ? `Nothing matches "${query.trim()}".` : "No clients yet."}
          </p>
        )}

        {tree.families.map((f) => {
          const expanded = isOpen(f.family.id);
          const isRenaming = renaming?.id === f.family.id;
          return (
            <div key={f.family.id}>
              <Row
                depth={0}
                expanded={expanded}
                onToggle={isRenaming ? undefined : () => toggle(f.family.id)}
                right={
                  <>
                    <Amount figures={f.figures} />
                    <div style={{ width: 84 }} />
                  </>
                }
              >
                <span style={{ font: "var(--type-h3)", color: "var(--text-primary)" }}>{f.name}</span>
                <span style={small}>
                  Family · {plural(f.members.length, "client")}
                  {f.figures.excluded > 0 && ` · ${f.figures.excluded} not counted`}
                </span>
              </Row>

              {expanded && (
                <>
                  {f.members.length === 0 ? (
                    <p style={{ ...small, margin: 0, padding: "10px 0 10px 50px", borderTop: "1px solid var(--border)" }}>
                      No clients yet. Open a client and choose this family.
                    </p>
                  ) : (
                    f.members.map((m) => clientRows(m, 1))
                  )}

                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", padding: "8px 0 12px 26px" }}>
                    {isRenaming ? (
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          const name = renaming.name.trim();
                          if (!name) return;
                          void run(async () => {
                            await api.patch(`/families/${f.family.id}`, { json: { name }, token });
                            setRenaming(null);
                          });
                        }}
                        style={{ display: "flex", gap: 8 }}
                      >
                        <Input value={renaming.name} onChange={(v) => setRenaming({ id: f.family.id, name: v })} />
                        <Button type="submit" size="sm" disabled={busy}>Save</Button>
                        <Button size="sm" variant="ghost" onClick={() => setRenaming(null)}>Cancel</Button>
                      </form>
                    ) : confirmDelete === f.family.id ? (
                      <>
                        <span style={small}>Remove this family? Its clients stay.</span>
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={busy}
                          onClick={() => run(() => api.delete(`/families/${f.family.id}`, { token }))}
                        >
                          Remove
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(null)}>Cancel</Button>
                      </>
                    ) : (
                      <>
                        <Button size="sm" variant="ghost" onClick={() => setRenaming({ id: f.family.id, name: f.family.name })}>
                          Rename
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(f.family.id)}>
                          Remove family
                        </Button>
                      </>
                    )}
                  </div>
                </>
              )}
            </div>
          );
        })}

        {tree.loose.map((m) => clientRows(m, 0))}
      </div>
    </div>
  );
}
