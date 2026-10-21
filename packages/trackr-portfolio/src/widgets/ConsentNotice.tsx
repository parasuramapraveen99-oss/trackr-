"use client";

import Link from "next/link";
import type { CSSProperties } from "react";
import { CONSENT_NOTICE_DETAILS, CONSENT_NOTICE_TEXT } from "@trackr/core";
import type { LegalRun } from "@trackr/core";

/**
 * The DPDP processing notice, shown wherever permission is asked: the onboarding permission
 * step in Trackr+ and the consent panel in both apps.
 *
 * ONE RENDERING OF ONE NOTICE. The words and the version live in `@trackr/core`'s
 * `consent.ts`; this only lays them out. A grant recorded against CONSENT_NOTICE_VERSION is
 * only valid if this exact notice was on screen when it was given, so every grant surface
 * must render this component rather than a paraphrase.
 */
export function ConsentNotice({ style }: { style?: CSSProperties }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        font: "var(--type-body-sm)",
        color: "var(--text-secondary)",
        background: "var(--surface-sunken)",
        border: "1px solid var(--border)",
        padding: "12px 14px",
        ...style,
      }}
    >
      <p style={{ margin: 0 }}>{CONSENT_NOTICE_TEXT}</p>
      {CONSENT_NOTICE_DETAILS.map((section) => (
        <div key={section.heading}>
          <p
            style={{
              margin: "0 0 4px",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              color: "var(--text-primary)",
            }}
          >
            {section.heading}
          </p>
          <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }}>
            {section.items.map((item, index) => (
              <li key={index}>
                {item.map((run, runIndex) => (
                  <NoticeRun key={runIndex} run={run} />
                ))}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function NoticeRun({ run }: { run: LegalRun }) {
  if (run.link) {
    // New tab: leaving mid-permission would abandon the step the person is on.
    return (
      <Link href={run.link} target="_blank" rel="noopener">
        {run.text}
      </Link>
    );
  }
  if (run.emphasis) return <strong>{run.text}</strong>;
  return <>{run.text}</>;
}
