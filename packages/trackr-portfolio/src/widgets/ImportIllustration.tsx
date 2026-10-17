/** Native vector artwork stays crisp in the upload panel and the setup dialog. */
export function ImportIllustration({ compact = false }: { compact?: boolean }) {
  return (
    <svg viewBox="0 0 360 220" width={compact ? 160 : 300} role="img" aria-label="Contract notes, CAS statements and tradebooks flowing into your portfolio" style={{ maxWidth: "100%", height: "auto", display: "block" }}>
      <ellipse cx="180" cy="191" rx="126" ry="13" fill="var(--surface-hover)" />
      <circle cx="179" cy="105" r="87" fill="var(--surface-sunken)" />
      <g stroke="var(--border-strong)" strokeWidth="1.5" fill="none">
        <path d="M32 93h37m-29 9h21M290 70h35m-25 10h31M61 158h24m198-5h33M274 33v12m-6-6h12M66 46v10m-5-5h10" />
        <circle cx="305" cy="124" r="4" /><circle cx="82" cy="28" r="3" />
      </g>
      <g transform="rotate(-13 126 103)">
        <rect x="80" y="43" width="89" height="118" rx="5" fill="var(--surface-raised)" stroke="var(--text-secondary)" strokeWidth="1.8" />
        <path d="M93 59h22" stroke="var(--ledger-ink-soft)" strokeWidth="4" />
        <text x="93" y="80" fontFamily="Arial,sans-serif" fontSize="10" fontWeight="bold" fill="var(--text-primary)">CONTRACT</text>
        <text x="93" y="94" fontFamily="Arial,sans-serif" fontSize="10" fontWeight="bold" fill="var(--text-primary)">NOTE</text>
        <path d="M93 108h60m-60 10h60m-60 10h40" stroke="var(--border-strong)" strokeWidth="3" />
      </g>
      <g transform="rotate(12 233 108)">
        <rect x="185" y="36" width="92" height="123" rx="5" fill="var(--surface-raised)" stroke="var(--text-secondary)" strokeWidth="1.8" />
        <path d="M199 53h22" stroke="var(--text-secondary)" strokeWidth="4" />
        <text x="199" y="75" fontFamily="Arial,sans-serif" fontSize="12" fontWeight="bold" fill="var(--text-primary)">MF CAS</text>
        <path d="M199 90h62m-62 11h62m-62 11h42" stroke="var(--border-strong)" strokeWidth="3" />
        <path d="M200 141v-12m12 12v-20m12 20v-15m12 15v-28" stroke="var(--text-tertiary)" strokeWidth="5" />
      </g>
      <path d="M84 126v43a9 9 0 0 0 9 9h171a9 9 0 0 0 9-9v-43l-87 48a13 13 0 0 1-13 0Z" fill="var(--surface-hover)" stroke="var(--text-secondary)" strokeWidth="1.8" />
      <path d="m88 174 55-35m126 35-56-35" fill="none" stroke="var(--border-strong)" strokeWidth="1.5" />
      <path d="M56 136c44 45 165 51 237-22l-3-10 30 4-15 26-3-11c-73 75-212 70-247 19Z" fill="var(--text-secondary)" stroke="var(--ledger-ink-soft)" strokeWidth="1.8" />
      <circle cx="286" cy="179" r="21" fill="var(--ink-900)" />
      <path d="m277 179 6 6 12-13" fill="none" stroke="var(--text-inverse)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function UploadIllustration() {
  return (
    <svg viewBox="0 0 120 104" width="106" height="92" aria-hidden="true">
      <rect x="24" y="13" width="61" height="73" rx="5" fill="var(--surface-hover)" stroke="var(--text-tertiary)" strokeWidth="1.5" transform="rotate(-8 54 49)" />
      <rect x="29" y="9" width="61" height="73" rx="5" fill="var(--surface-raised)" stroke="var(--text-secondary)" strokeWidth="1.5" />
      <path d="M43 27h24m-24 9h31m-31 9h31m-31 9h19" stroke="var(--border-strong)" strokeWidth="4" strokeLinecap="round" />
      <circle cx="84" cy="76" r="20" fill="var(--ink-900)" />
      <path d="M84 86V66m-8 8 8-8 8 8" fill="none" stroke="var(--text-inverse)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M14 67v24h45" fill="none" stroke="var(--border-strong)" strokeDasharray="2 4" strokeWidth="2" />
    </svg>
  );
}
