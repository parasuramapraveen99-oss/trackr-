"use client";

import { createPortal } from "react-dom";
import { useEffect, useMemo, useState } from "react";
import type { ExpectedDividendOut, UnprojectableDividendOut } from "@trackr/core";
import { formatDate, formatMoney, formatQuantity } from "@trackr/core";
import { EmptyState, IconButton, Skeleton } from "@trackr/ui";

const UNPROJECTABLE_REASON: Record<string, string> = {
  UNCORROBORATED:
    "Only one source has reported this declaration so far — the normal resting state while it awaits independent corroboration.",
  SHARE_COUNT_CHANGED:
    "A bonus or split landed close enough before the ex-date that the declared amount per share and the held quantity may be on different bases.",
};

const sectionTitle = {
  margin: "0 0 10px",
  font: "var(--type-label)",
  textTransform: "uppercase" as const,
  letterSpacing: ".08em",
  color: "var(--text-tertiary)",
};

const row = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  gap: 12,
  padding: "12px 0",
  borderBottom: "1px solid var(--border)",
};

function EventRow({ event, label }: { event: ExpectedDividendOut; label: (isin: string) => string }) {
  return (
    <div style={row}>
      <div>
        <p style={{ margin: 0, font: "var(--type-body)", color: "var(--text-primary)" }}>{label(event.isin)}</p>
        <p style={{ margin: "3px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          {event.kind} · ex-date {formatDate(event.ex_date)}
          {event.payment_date ? ` · pays ${formatDate(event.payment_date)}` : ""}
        </p>
        <p style={{ margin: "2px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          {/* An amount per share is paise-significant, so it is the PRECISE form; the
              held quantity goes through the shared quantity formatter. Neither is parsed
              into a number first -- `Number(s).toFixed(2)` and
              `Number(q).toLocaleString()` were both doing exactly that. */}
          {formatMoney(event.amount_per_share, true)}/share ×{" "}
          {formatQuantity(event.eligible_quantity)} held
        </p>
      </div>
      <span
        className="tabular-nums"
        style={{ font: "var(--type-body)", fontWeight: 600, color: "var(--text-primary)", whiteSpace: "nowrap" }}
      >
        {formatMoney(event.gross_expected)}
      </span>
    </div>
  );
}

function UnprojectableRow({ item, label }: { item: UnprojectableDividendOut; label: (isin: string) => string }) {
  return (
    <div style={{ padding: "12px 0", borderBottom: "1px solid var(--border)" }}>
      <p style={{ margin: 0, font: "var(--type-body)", color: "var(--text-primary)" }}>
        {label(item.isin)} <span style={{ color: "var(--text-tertiary)" }}>· ex-date {formatDate(item.ex_date)}</span>
      </p>
      <p style={{ margin: "3px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        {UNPROJECTABLE_REASON[item.code] ?? item.detail}
      </p>
    </div>
  );
}

/**
 * Declared dividends, sized to this investor's holdings — the full list behind the
 * dashboard's "Announced" figure.
 *
 * Reads the same `/dividends/expected` response `DividendCard` already fetched, passed in
 * as props: nothing here issues its own request, so opening the drawer never doubles the
 * round trip DividendCard already made. `asOf` splits the list at the same boundary the
 * card's figure respects — upcoming vs. already gone ex-dividend — so the two never
 * disagree about what "announced" includes.
 *
 * Every figure shown is `gross_expected`: a projection, never income. See
 * `docs/ARCHITECTURE_INVARIANTS.md` #18 — nothing here may be summed into Received.
 */
export function DividendDrawer({
  open,
  onClose,
  loading,
  error,
  expected,
  unprojectable,
  names,
  asOf,
}: {
  open: boolean;
  onClose: () => void;
  loading: boolean;
  error?: string | null;
  expected: ExpectedDividendOut[];
  unprojectable: UnprojectableDividendOut[];
  names?: Record<string, string>;
  asOf?: string;
}) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const label = (isin: string) => names?.[isin] ?? isin;

  const sorted = useMemo(
    () => expected.slice().sort((a, b) => a.ex_date.localeCompare(b.ex_date)),
    [expected]
  );
  const upcoming = useMemo(
    () => (asOf ? sorted.filter((e) => e.ex_date > asOf) : sorted),
    [sorted, asOf]
  );
  const pastExDate = useMemo(
    () => (asOf ? sorted.filter((e) => e.ex_date <= asOf).reverse() : []),
    [sorted, asOf]
  );

  if (!mounted || !open) return null;

  const nothing = !loading && !error && expected.length === 0 && unprojectable.length === 0;

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "flex",
        justifyContent: "flex-end",
        background: "var(--overlay-scrim)",
        animation: "trackr-fade-in var(--duration-base) ease both",
      }}
    >
      <div onClick={onClose} style={{ position: "absolute", inset: 0 }} aria-hidden="true" />
      <div
        style={{
          position: "relative",
          display: "flex",
          flexDirection: "column",
          width: 620,
          maxWidth: "100%",
          height: "100%",
          background: "var(--surface)",
          borderLeft: "1px solid var(--border)",
          boxShadow: "var(--shadow-overlay)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 12,
            padding: "18px 20px",
            borderBottom: "1px solid var(--border)",
          }}
        >
          <div>
            <p
              style={{
                margin: 0,
                font: "var(--type-h3)",
                fontFamily: "var(--font-heading)",
                color: "var(--text-primary)",
              }}
            >
              Declared dividends
            </p>
            <p style={{ margin: "2px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
              Announced payouts on your current holdings, sized to what you held at each ex-date.
            </p>
          </div>
          <IconButton name="close" label="Close" variant="plain" size={32} onClick={onClose} />
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: 20 }}>
          {loading ? (
            <>
              <Skeleton width="100%" height={56} style={{ marginBottom: 10 }} />
              <Skeleton width="100%" height={56} style={{ marginBottom: 10 }} />
              <Skeleton width="100%" height={56} />
            </>
          ) : error ? (
            <p style={{ margin: 0, font: "var(--type-body)", color: "var(--negative)" }}>{error}</p>
          ) : nothing ? (
            <EmptyState
              title="Nothing declared"
              hint="No verified declaration currently covers a security you held at its ex-date. This list fills in as companies announce dividends on your holdings."
            />
          ) : (
            <>
              {upcoming.length > 0 && (
                <div style={{ marginBottom: 24 }}>
                  <p style={sectionTitle}>Upcoming</p>
                  {upcoming.map((e) => (
                    <EventRow key={e.event_id ?? `${e.isin}-${e.ex_date}`} event={e} label={label} />
                  ))}
                </div>
              )}
              {pastExDate.length > 0 && (
                <div style={{ marginBottom: 24 }}>
                  <p style={sectionTitle}>Already gone ex-dividend</p>
                  {pastExDate.map((e) => (
                    <EventRow key={e.event_id ?? `${e.isin}-${e.ex_date}`} event={e} label={label} />
                  ))}
                </div>
              )}
              {unprojectable.length > 0 && (
                <div>
                  <p style={sectionTitle}>Declared, not yet sized</p>
                  {unprojectable.map((u) => (
                    <UnprojectableRow key={`${u.isin}-${u.ex_date}`} item={u} label={label} />
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        <div style={{ padding: "14px 20px", borderTop: "1px solid var(--border)" }}>
          <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
            Projections, not income — none of this is counted in Received, XIRR, TWRR or
            contribution until it actually arrives and is recorded as a dividend transaction.
          </p>
        </div>
      </div>
    </div>,
    document.body
  );
}
