"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { PageHeader } from "../components/PageHeader";
import { PageBody } from "../components/Shell";
import { LandingHeader } from "../components/landing/LandingHeader";
import { LandingFooter } from "../components/landing/LandingFooter";
import "../components/landing/landing.css";
import type { CSSProperties, ReactNode } from "react";
import {
  CURRENT_TOS_VERSION,
  TERMS_INTRO,
  TERMS_SECTIONS,
  type LegalBlock,
  type LegalRun,
} from "@trackr/core";

/**
 * The Terms of Service and Privacy Policy a login accepts at registration.
 *
 * THE WORDS MOVED, THE APPEARANCE DID NOT. Every sentence now comes from `@trackr/core`'s
 * `legal.ts`, because Trackr Workspace serves this page too and one backend with one
 * `app_user.tos_version` column must not have two hand-maintained copies of its legal text --
 * an edit applied to one app and not the other means two accounts agreed to different text
 * under the same version string. What this file still owns is the rendering: Plus's tokens,
 * Plus's type scale, Plus's spacing, all unchanged.
 *
 * ⚠️ NOT LEGALLY REVIEWED. Every statement below is drawn from what this codebase
 * actually does — docs/COMPLIANCE.md, docs/INGESTION.md, and the architectural rules in
 * CLAUDE.md — and is accurate as a description of the software. It is NOT a substitute
 * for a lawyer reading it before real clients are onboarded, and docs/COMPLIANCE.md §4
 * lists that as a go-live gate. Do not treat this file as the compliance work being done.
 *
 * WHY IT IS SHORT AND CONCRETE. The commitments that carry weight here are technical
 * facts enforced by the code — no broker credentials are stored because there is no table
 * to store them in and `make lint` fails the build if one appears; the ledger is
 * append-only because a database trigger refuses UPDATE and DELETE. Terms that describe
 * things the software genuinely cannot do are worth more than terms that promise
 * restraint.
 *
 * VERSIONED, and the version is the point. `CURRENT_TOS_VERSION` is sent with the
 * registration and stamped onto `app_user.tos_version`, so an account records which text
 * it agreed to rather than a bare boolean. Change the text, bump the version — in
 * packages/trackr-core/src/legal.ts AND trackr-backend/app/terms.py, which the server checks
 * against. `@/lib/terms` re-exports the value, so existing import paths still work.
 */
export default function TermsPage() {
  const { token } = useAuth();
  return (
    <div className={token ? undefined : "l-root"}>
      {!token && <LandingHeader />}
      <PageHeader title="Terms of Service & Privacy Policy" subtitle={`Trackr+ · version ${CURRENT_TOS_VERSION}`} />
      <PageBody column>
      <p style={{ ...bodyStyle, marginTop: 12, color: "var(--text-secondary)" }}>
        {TERMS_INTRO.map((run, index) => (
          <Run key={index} run={run} />
        ))}
      </p>

      {TERMS_SECTIONS.map((section) => (
        <Section key={section.title} title={section.title}>
          {section.blocks.map((block, index) => (
            <Block key={index} block={block} />
          ))}
        </Section>
      ))}

      <p style={{ ...bodyStyle, marginTop: 32, color: "var(--text-tertiary)" }}>
        <Link href={token ? "/settings#privacy" : "/register"}>{token ? "← Back to Settings" : "← Back to sign up"}</Link>
      </p>
      </PageBody>
      {!token && <LandingFooter />}
    </div>
  );
}

function Block({ block }: { block: LegalBlock }) {
  if (block.kind === "list") {
    return (
      <List
        items={block.runs.map((item, index) => (
          <span key={index}>
            {item.map((run, runIndex) => (
              <Run key={runIndex} run={run} />
            ))}
          </span>
        ))}
      />
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

/**
 * One run of text.
 *
 * `link` is always an internal path — the shared content carries no external or cross-product
 * URLs — so it is safe to hand straight to `next/link` and stays on this origin.
 */
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

const eyebrowStyle: CSSProperties = {
  margin: 0,
  font: "var(--type-label)",
  textTransform: "uppercase",
  letterSpacing: "var(--tracking-label)",
  color: "var(--text-tertiary)",
};

const bodyStyle: CSSProperties = {
  margin: "0 0 12px",
  font: "var(--type-body)",
  color: "var(--text-secondary)",
  lineHeight: 1.65,
};

function Section({ title, children }: { title: string; children: ReactNode }) {
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

function List({ items }: { items: ReactNode[] }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 10 }}>
      {items.map((item, index) => (
        <li key={index} style={{ ...bodyStyle, margin: 0 }}>
          {item}
        </li>
      ))}
    </ul>
  );
}
