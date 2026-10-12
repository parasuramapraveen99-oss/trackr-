"use client";

import { useState } from "react";
import { formatDate, formatPercent, signClass } from "@/lib/format";

// Rates are exact decimal strings on the wire (BE 02) and stay strings here: a window
// switcher shows and colours them, and neither needs a number to do it.
type TrailingWindow = { key: string; label: string; value: string | null; twrr: string | null };

/**
 * XIRR by trailing window -- FY, 1Y, 3Y, 5Y -- with TWRR beside the selected one.
 *
 * **This card deliberately does NOT show xirr_all / xirr_holdings.** That canonical
 * since-inception pair is the home dashboard's headline figure (app/page.tsx), and it used
 * to be rendered in both places. Two identical analytical cards means two places to change
 * when one of them is wrong, and they drift apart. The split: home answers "how am I
 * actually doing" with one pair of numbers; this tab is the drill-down across spans.
 *
 * Windows with a null value are OMITTED rather than shown blank -- a portfolio younger
 * than 5 years legitimately has no xirr_5y (see app.routers.analytical._window_xirr), so
 * only windows with a real figure get a tab. `period_xirr` (adviser onboarding) is not one
 * of them: it is anchored to an onboarding date rather than a trailing span, so it reads as
 * its own line rather than a switchable window.
 *
 * TWRR (fy_twrr/twrr_1y/twrr_3y/twrr_5y, docs/ENGINE_SPEC.md section 5) rides beside the
 * active window rather than getting its own card, because it answers a different question
 * over the SAME window -- did the strategy work, versus did the client's actual
 * money-weighted experience work. The two are meant to be read together, not switched
 * between. Omitted, not blanked, when null.
 */
export function XirrWindowCard({
  fyXirr,
  xirr1y,
  xirr3y,
  xirr5y,
  fyTwrr,
  twrr1y,
  twrr3y,
  twrr5y,
  periodXirr,
  periodStart,
}: {
  fyXirr: string | null;
  xirr1y: string | null;
  xirr3y: string | null;
  xirr5y: string | null;
  fyTwrr: string | null;
  twrr1y: string | null;
  twrr3y: string | null;
  twrr5y: string | null;
  periodXirr: string | null;
  periodStart: string | null;
}) {
  const windows: TrailingWindow[] = [
    { key: "fy", label: "FY", value: fyXirr, twrr: fyTwrr },
    { key: "1y", label: "1Y", value: xirr1y, twrr: twrr1y },
    { key: "3y", label: "3Y", value: xirr3y, twrr: twrr3y },
    { key: "5y", label: "5Y", value: xirr5y, twrr: twrr5y },
  ].filter((w) => w.value !== null);

  const [selectedKey, setSelectedKey] = useState("fy");
  const active = windows.find((w) => w.key === selectedKey) ?? windows[0];

  return (
    <div className="glass-card glass-card-hover animate-fade-in-up rounded-xl border border-navy-100 p-5 sm:col-span-2">
      <p className="text-[11px] font-medium uppercase tracking-wide text-navy-400">
        XIRR · by window
      </p>
      {active ? (
        <div className="mt-2">
          <p className={`font-heading text-2xl tabular-nums ${signClass(active.value)}`}>
            {formatPercent(active.value)}
          </p>
          <p className="mt-1 text-xs text-navy-400">
            {active.label} · cash-flow, all external flows
          </p>
          <div className="mt-3 flex gap-1 border-t border-navy-100 pt-2">
            {windows.map((w) => (
              <button
                key={w.key}
                type="button"
                onClick={() => setSelectedKey(w.key)}
                className={`rounded px-2 py-0.5 text-[11px] font-medium ${
                  w.key === active.key
                    ? "bg-brass-100 text-navy-700"
                    : "text-navy-400 hover:text-navy-600"
                }`}
              >
                {w.label}
              </button>
            ))}
          </div>
          {active.twrr !== null && (
            <p className="mt-2 text-xs text-navy-600">
              TWRR over the same window:{" "}
              <span className={`font-medium tabular-nums ${signClass(active.twrr)}`}>
                {formatPercent(active.twrr)}
              </span>
            </p>
          )}
        </div>
      ) : (
        <p className="mt-2 text-xs text-navy-400">
          Not enough history for any trailing window yet.
        </p>
      )}
      {periodXirr !== null && (
        <p className="mt-1 text-xs text-navy-600">
          Since {formatDate(periodStart)}:{" "}
          <span className="font-medium tabular-nums">{formatPercent(periodXirr)}</span>
        </p>
      )}
    </div>
  );
}
