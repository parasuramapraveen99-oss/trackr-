"use client";

import Link from "next/link";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

/**
 * Where a not-yet-built link goes — the landing page's Pricing, About, Blog, Contact,
 * Status and Compliance links today. `?feature=` names what's missing; unset or blank
 * falls back to "This page" rather than rendering an empty heading.
 */
function ComingSoonBody() {
  const feature = useSearchParams().get("feature")?.trim() || "This page";

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 16,
        padding: "24px",
        background: "var(--page)",
        textAlign: "center",
      }}
    >
      <span
        style={{
          fontFamily: "var(--font-label)",
          fontSize: 11,
          fontWeight: 600,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: "var(--text-tertiary)",
        }}
      >
        Coming soon
      </span>
      <h1
        style={{
          margin: 0,
          fontFamily: "var(--font-heading)",
          fontSize: 32,
          fontWeight: 500,
          color: "var(--text-primary)",
        }}
      >
        {feature}
      </h1>
      <Link
        href="/"
        style={{
          marginTop: 8,
          fontSize: 14,
          color: "var(--text-link)",
        }}
      >
        Back to Trackr+
      </Link>
    </main>
  );
}

export default function ComingSoonPage() {
  return (
    <Suspense fallback={null}>
      <ComingSoonBody />
    </Suspense>
  );
}
