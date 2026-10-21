"use client";

import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import {
  CURRENT_TOS_VERSION,
  LEGAL_EFFECTIVE_DATE,
  type LegalBlock,
  type LegalRun,
  type LegalSection,
} from "@trackr/core";
import { useAuth } from "@/lib/auth-context";
import { PageHeader } from "./PageHeader";
import { PageBody } from "./Shell";
import { LandingHeader } from "./landing/LandingHeader";
import { LandingFooter } from "./landing/LandingFooter";
import "./landing/landing.css";

/**
 * One legal page in Plus's visual language: /terms, /privacy-policy and /grievance.
 *
 * Signed in, the page sits inside the app's own chrome (Shell.tsx SHELLED_WHEN_SIGNED_IN adds
 * the ribbon; this adds the page header). Signed out, it sits between the landing header and
 * footer, so a visitor deciding whether to sign up stays on the marketing site.
 *
 * The WORDS come from `@trackr/core`'s `legal.ts` (shared with Workspace, versioned against
 * `app_user.tos_version`); this file owns only the rendering. Every `link` in the shared text
 * is an internal path, so it is safe to hand straight to `next/link`.
 */
export function LegalDocument({
  title,
  intro,
  sections,
  children,
}: {
  title: string;
  intro: LegalRun[];
  sections: LegalSection[];
  children?: ReactNode;
}) {
  const { token } = useAuth();
  return (
    <div className={token ? undefined : "l-root"}>
      {!token && <LandingHeader />}
      <PageHeader
        title={title}
        subtitle={`Trackr+ · version ${CURRENT_TOS_VERSION} · effective ${LEGAL_EFFECTIVE_DATE}`}
      />
      <PageBody column>
        <p style={{ ...bodyStyle, marginTop: 12, color: "var(--text-secondary)" }}>
          {intro.map((run, index) => (
            <Run key={index} run={run} />
          ))}
        </p>

        {sections.map((section) => (
          <Section key={section.title} title={section.title}>
            {section.blocks.map((block, index) => (
              <Block key={index} block={block} />
            ))}
          </Section>
        ))}

        {children}

        <p style={{ ...bodyStyle, marginTop: 32, color: "var(--text-tertiary)" }}>
          <Link href="/terms">Terms of Use</Link> · <Link href="/privacy-policy">Privacy Policy</Link> ·{" "}
          <Link href="/grievance">Grievance</Link> ·{" "}
          <Link href={token ? "/settings#privacy" : "/register"}>{token ? "Settings" : "Sign up"}</Link>
        </p>
      </PageBody>
      {!token && <LandingFooter />}
    </div>
  );
}

function Block({ block }: { block: LegalBlock }) {
  if (block.kind === "list") {
    return (
      <ul style={{ margin: "0 0 12px", paddingLeft: 18, display: "grid", gap: 10 }}>
        {block.runs.map((item, index) => (
          <li key={index} style={{ ...bodyStyle, margin: 0 }}>
            {item.map((run, runIndex) => (
              <Run key={runIndex} run={run} />
            ))}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <p style={bodyStyle}>
      {block.runs[0]?.map((run, index) => (
        <Run key={index} run={run} />
      ))}
    </p>
  );
}

function Run({ run }: { run: LegalRun }) {
  if (run.link) return <Link href={run.link}>{run.text}</Link>;
  if (run.code) {
    return (
      <code style={{ font: "var(--type-mono)", color: "var(--text-primary)" }}>{run.text}</code>
    );
  }
  if (run.emphasis) return <strong>{run.text}</strong>;
  return <>{run.text}</>;
}

export const legalBodyStyle: CSSProperties = {
  margin: "0 0 12px",
  font: "var(--type-body)",
  color: "var(--text-secondary)",
  lineHeight: 1.65,
};
const bodyStyle = legalBodyStyle;

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section style={{ marginTop: 32, borderTop: "1px solid var(--border)", paddingTop: 20 }}>
      <h2
        style={{
          margin: "0 0 12px",
          font: "var(--type-h2)",
          color: "var(--text-primary)",
        }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}
