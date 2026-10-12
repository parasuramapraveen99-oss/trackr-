"use client";

import { useState } from "react";

/** Password `<input>` with a show/hide toggle -- icon style matches
 * onboarding/WizardShell.tsx's Back/Close glyphs (20x20 viewBox, stroke-only,
 * currentColor) so it reads as the same icon set app-wide. */
export function PasswordInput({
  value,
  onChange,
  required,
  minLength,
  autoComplete,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  minLength?: number;
  autoComplete?: string;
  id?: string;
}) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="relative">
      <input
        id={id}
        type={visible ? "text" : "password"}
        required={required}
        minLength={minLength}
        autoComplete={autoComplete}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-navy-100 bg-surface px-3 py-2 pr-10 text-sm focus:border-brass-500 focus:outline-none"
      />
      <button
        type="button"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        tabIndex={-1}
        className="absolute inset-y-0 right-0 flex items-center px-3 text-navy-400 hover:text-navy-700"
      >
        {visible ? (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4">
            <path d="M2.5 10s2.5-5.5 7.5-5.5S17.5 10 17.5 10s-2.5 5.5-7.5 5.5S2.5 10 2.5 10Z" />
            <circle cx="10" cy="10" r="2.25" />
            <path d="M3 3l14 14" />
          </svg>
        ) : (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4">
            <path d="M2.5 10s2.5-5.5 7.5-5.5S17.5 10 17.5 10s-2.5 5.5-7.5 5.5S2.5 10 2.5 10Z" />
            <circle cx="10" cy="10" r="2.25" />
          </svg>
        )}
      </button>
    </div>
  );
}
