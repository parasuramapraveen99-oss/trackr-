"use client";

import { useState } from "react";

/** navigator.clipboard requires a secure context; execCommand is the fallback for
 * plain-HTTP local dev over a LAN IP where it's unavailable. */
export function CopyField({ value, label }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const el = document.createElement("textarea");
        el.value = value;
        el.style.position = "fixed";
        el.style.opacity = "0";
        document.body.appendChild(el);
        el.select();
        document.execCommand("copy");
        document.body.removeChild(el);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
      <code title={value} style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere", whiteSpace: "normal", userSelect: "all", border: "1px solid var(--border)", background: "var(--surface-sunken)", padding: "9px 10px", font: "var(--type-label)", fontSize: 12, lineHeight: 1.6, fontFamily: "var(--font-mono, monospace)", color: "var(--text-primary)" }}>
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={label ? `Copy ${label}` : "Copy"}
        style={{ flexShrink: 0, border: "1px solid var(--border)", background: "var(--surface)", padding: "9px 10px", color: "var(--text-secondary)", font: "var(--type-label)", cursor: "pointer" }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
