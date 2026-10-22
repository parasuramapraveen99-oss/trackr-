"use client";

import type { ReactNode } from "react";

/**
 * Step 1's "what you get" glimpse. Each tile is a drawing, a title and one plain sentence.
 * The drawings are deliberately numberless: nothing here may read as the person's own
 * figures before they have imported anything.
 */
const STROKE = "var(--text-secondary)";

const FEATURES: { title: string; text: string; art: ReactNode }[] = [
  {
    title: "All your brokers in one place",
    text: "Add tradebooks, contract notes and CAS from any broker.",
    art: (
      <>
        {[0, 1, 2].map((i) => <rect key={i} x={8 + i * 14} y={10 + i * 6} width={26} height={34} rx={3} fill="var(--surface-raised)" stroke={STROKE} strokeWidth={1.4} />)}
        <path d="M70 34h18m-6-6 6 6-6 6" fill="none" stroke={STROKE} strokeWidth={1.6} strokeLinecap="round" />
        <circle cx={118} cy={34} r={18} fill="none" stroke="var(--chart-1)" strokeWidth={8} strokeDasharray="40 100" />
        <circle cx={118} cy={34} r={18} fill="none" stroke="var(--chart-3)" strokeWidth={8} strokeDasharray="30 100" strokeDashoffset={-42} />
        <circle cx={118} cy={34} r={18} fill="none" stroke="var(--chart-2)" strokeWidth={8} strokeDasharray="38 100" strokeDashoffset={-74} />
      </>
    ),
  },
  {
    title: "Your real return vs Nifty",
    text: "See how you did against Nifty, with the same money on the same dates.",
    art: (
      <>
        <path d="M8 56h132" stroke="var(--border-strong)" />
        <path d="M8 48c18-4 26-14 42-12s24-16 40-14 32-10 50-14" fill="none" stroke="var(--ink-400)" strokeWidth={1.6} strokeDasharray="4 3" />
        <path d="M8 50c16-2 28-6 42-10s28-22 44-20 28-12 46-16" fill="none" stroke="var(--chart-1)" strokeWidth={2.4} />
      </>
    ),
  },
  {
    title: "Portfolio health check",
    text: "Spot too much money in one stock and how bumpy the ride has been.",
    art: (
      <>
        {[["var(--chart-1)", 25], ["var(--chart-2)", 15], ["var(--chart-4)", 30], ["var(--chart-3)", 20], ["var(--chart-6)", 10]].reduce<{ x: number; els: ReactNode[] }>((acc, [c, w], i) => {
          const width = (Number(w) / 100) * 124;
          acc.els.push(<rect key={i} x={12 + acc.x} y={24} width={width - 2} height={16} rx={3} fill={String(c)} opacity={i === 4 ? 0.35 : 0.9} />);
          acc.x += width;
          return acc;
        }, { x: 0, els: [] }).els}
        <path d="M100 18v28" stroke="var(--text-primary)" strokeWidth={2} />
        <circle cx={100} cy={16} r={3} fill="var(--text-primary)" />
      </>
    ),
  },
  {
    title: "Tax made simple",
    text: "Capital gains worked out for your tax return, including Schedule 112A.",
    art: (
      <>
        <rect x={44} y={6} width={60} height={50} rx={4} fill="var(--surface-raised)" stroke={STROKE} strokeWidth={1.4} />
        <text x={54} y={24} fontSize={12} fontWeight={700} fill="var(--text-primary)" fontFamily="Arial,sans-serif">₹</text>
        {[30, 38, 46].map((y) => <path key={y} d={`M54 ${y}h30`} stroke="var(--border-strong)" strokeWidth={3} strokeLinecap="round" />)}
        <circle cx={100} cy={50} r={10} fill="var(--positive)" />
        <path d="m95 50 3.5 3.5 6.5-7" fill="none" stroke="#fff" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
      </>
    ),
  },
  {
    title: "Bonus and splits handled",
    text: "Bonus shares, splits and ISIN changes are adjusted for you automatically.",
    art: (
      <>
        <rect x={20} y={20} width={30} height={30} rx={4} fill="var(--chart-1)" opacity={0.85} />
        <path d="M58 35h20m-6-6 6 6-6 6" fill="none" stroke={STROKE} strokeWidth={1.6} strokeLinecap="round" />
        <rect x={88} y={12} width={22} height={22} rx={3} fill="var(--chart-1)" opacity={0.85} />
        <rect x={114} y={12} width={22} height={22} rx={3} fill="var(--chart-1)" opacity={0.85} />
        <rect x={88} y={38} width={22} height={22} rx={3} fill="var(--chart-1)" opacity={0.85} />
        <rect x={114} y={38} width={22} height={22} rx={3} fill="var(--chart-1)" opacity={0.5} />
      </>
    ),
  },
  {
    title: "Dividends you're owed",
    text: "See dividends announced for the shares you hold.",
    art: (
      <>
        <rect x={20} y={10} width={56} height={46} rx={4} fill="var(--surface-raised)" stroke={STROKE} strokeWidth={1.4} />
        <path d="M20 22h56" stroke={STROKE} strokeWidth={1.4} />
        {[0, 1, 2, 3].map((i) => <rect key={i} x={26 + i * 12} y={30} width={8} height={8} rx={1.5} fill={i === 2 ? "var(--positive)" : "var(--border-strong)"} />)}
        {[0, 1, 2].map((i) => <ellipse key={i} cx={112} cy={50 - i * 7} rx={16} ry={5} fill="var(--warning)" stroke="var(--surface)" strokeWidth={1} />)}
      </>
    ),
  },
  {
    title: "Forward once, stay updated",
    text: "Forward your broker emails and new trades are added for you.",
    art: (
      <>
        <rect x={16} y={14} width={52} height={36} rx={4} fill="var(--surface-raised)" stroke={STROKE} strokeWidth={1.4} />
        <path d="m16 16 26 18 26-18" fill="none" stroke={STROKE} strokeWidth={1.4} />
        <path d="M76 32c14-10 28-10 40 0" fill="none" stroke="var(--chart-1)" strokeWidth={1.8} strokeDasharray="3 3" />
        <path d="m112 26 5 6-7 2" fill="none" stroke="var(--chart-1)" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
        <circle cx={128} cy={40} r={11} fill="var(--ink-900)" />
        <path d="m123 40 3.5 3.5 6.5-7" fill="none" stroke="var(--text-inverse)" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
      </>
    ),
  },
];

export function FeatureShowcase() {
  return (
    <section aria-label="What Trackr+ gives you" style={{ display: "grid", gap: 16 }}>
      <style>{`
        .fs-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
        .fs-tile { display: grid; gap: 8px; align-content: start; padding: 14px; background: var(--surface); border: 1px solid var(--border); border-top: 1px solid var(--ledger-rule, var(--border)); box-shadow: var(--shadow-slip, none); }
        .fs-tile svg { width: 100%; height: 64px; display: block; background: var(--surface-sunken); }
        .fs-tile h4 { margin: 4px 0 0; font: var(--type-body); font-weight: 600; color: var(--text-primary); }
        .fs-tile p { margin: 0; font: var(--type-body-sm); line-height: 1.5; color: var(--text-secondary); }
      `}</style>
      <div className="fs-grid">
        {FEATURES.map((f) => (
          <article key={f.title} className="fs-tile">
            <svg viewBox="0 0 148 64" preserveAspectRatio="xMidYMid meet" aria-hidden="true">{f.art}</svg>
            <h4>{f.title}</h4>
            <p>{f.text}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
