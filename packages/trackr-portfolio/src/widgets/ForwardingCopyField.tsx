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
    <div className="flex items-center gap-2">
      <code className="flex-1 truncate rounded-lg border border-navy-100 bg-surface px-3 py-2 text-sm font-mono text-navy-800">
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={label ? `Copy ${label}` : "Copy"}
        className="press-scale shrink-0 rounded-lg border border-navy-100 px-3 py-2 text-xs font-medium text-navy-600 transition-colors hover:bg-cream-50"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
