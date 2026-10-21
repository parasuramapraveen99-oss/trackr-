"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AccountOut, BrokerOut } from "@trackr/core";
import { Button, Icon } from "@trackr/ui";
import styles from "./TradebookGuide.module.css";

const ZERODHA_STEPS = [
  { title: "Sign in to Zerodha Console", text: "Open console.zerodha.com in your browser and sign in to your own account.", focus: "Console" },
  { title: "Open Reports → Tradebook", text: "Choose Reports in the top navigation, then open Tradebook.", focus: "Tradebook" },
  { title: "Choose a segment and date range", text: "Choose Equity for your stock trades. Select the dates you want to import. Each export can cover at most 365 days.", focus: "Date range" },
  { title: "Download the CSV", text: "Run the report, then download the CSV. Repeat for each year, starting with your earliest trades.", focus: "Download CSV" },
  { title: "Upload your downloaded files", text: "Return to Trackr+ and upload your CSV or XLSX tradebooks together. Upload contract notes too, to include brokerage and taxes.", focus: "Upload to Trackr+" },
];

/** Illustrative broker screens, with instructions supplied by the broker registry. */
export function TradebookGuide({ accounts, brokers }: { accounts: AccountOut[]; brokers: BrokerOut[] }) {
  const mine = brokers.filter((broker) => accounts.some((account) => account.broker.toUpperCase() === broker.code));
  const choices = mine.length ? mine : brokers;
  const [open, setOpen] = useState(false);
  const [brokerCode, setBrokerCode] = useState("");
  const [step, setStep] = useState(0);
  const titleId = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const broker = choices.find((item) => item.code === brokerCode) ?? choices[0];
  const steps = broker?.code === "ZERODHA" ? ZERODHA_STEPS : [
    ...(broker?.console_steps ?? []).map((text, index) => ({ title: index === 0 ? `Open ${broker?.name}` : `Download your ${broker?.name} files`, text, focus: index === 0 ? "Sign in" : "Reports & downloads" })),
    { title: "Upload your downloaded files", text: "Return to Trackr+ and upload your downloaded statements together.", focus: "Upload to Trackr+" },
  ];
  const current = steps[step] ?? steps[0]!;

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    function keydown(event: KeyboardEvent) {
      // Capture before the underlying onboarding dialog's Escape handler.
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); setOpen(false); }
      if (event.key !== "Tab") return;
      const items = dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], select:not([disabled])');
      if (!items?.length) return;
      const first = items[0]!; const last = items[items.length - 1]!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    window.addEventListener("keydown", keydown, true);
    return () => { document.body.style.overflow = overflow; window.removeEventListener("keydown", keydown, true); previous?.focus(); };
  }, [open]);

  if (!choices.length) return null;
  return <>
    <div className={styles.help}><Icon name="download" size={16} /><span>Need your tradebook?</span><Button variant="ghost" size="sm" onClick={() => { setStep(0); setOpen(true); }}>View download guide</Button></div>
    {open && createPortal(<div className={styles.overlay} onClick={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
      <div ref={dialog} className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className={styles.header}><div><span className={styles.eyebrow}>TRADEBOOK DOWNLOAD GUIDE</span><h2 id={titleId}>Download your trade history</h2></div><button ref={closeButton} className={styles.close} type="button" onClick={() => setOpen(false)} aria-label="Close tradebook guide"><Icon name="close" size={18} /></button></header>
        <div className={styles.body}>
          <label className={styles.broker}>Your broker<select value={broker?.code} onChange={(event) => { setBrokerCode(event.target.value); setStep(0); }}>{choices.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
          <div className={styles.layout}>
            <nav className={styles.steps} aria-label="Download steps">{steps.map((item, index) => <button key={index} type="button" aria-current={index === step ? "step" : undefined} onClick={() => setStep(index)}><span>{index + 1}</span>{item.title}</button>)}</nav>
            <div className={styles.instruction}>
              <div className={styles.visual} role="img" aria-label={`Illustration of ${current.title}: ${current.text}`}>
                <div className={styles.browser}><i /><i /><i /><span>{step === steps.length - 1 ? "Trackr+ · Import" : `${broker?.name} · Reports`}</span><small>ILLUSTRATION</small></div>
                <div className={styles.screen} aria-hidden="true"><div className={styles.mockNav}><b>{step === steps.length - 1 ? "Trackr+" : broker?.name}</b><span>Dashboard</span><span className={step === 1 ? styles.highlight : ""}>Reports</span></div>
                  {step === steps.length - 1 ? <div className={styles.upload}><Icon name="upload" size={28} /><strong>Upload your tradebook</strong><span>{broker?.code === "ZERODHA" ? "CSV · XLSX" : "Broker statements"}</span></div> : <div className={styles.mockReport}>
                    <h3>{step === 0 ? "Your account" : "Tradebook"}</h3>
                    {step === 0 ? <div className={styles.login}>Sign in to your broker account<div className={styles.highlight}>Continue to {broker?.name}</div></div> : <><div className={styles.filters}><span>Segment<strong>Equity ▾</strong></span><span className={step === 2 ? styles.highlight : ""}>Date range<strong>01 Apr 2025 → 31 Mar 2026</strong></span></div><div className={styles.mockTable}><div>Trade date　　Security　　Quantity　　Price</div>{[1, 2, 3].map((row) => <div key={row}><span /><span /><span /><span /></div>)}</div><div className={styles.download}><span className={step === 3 ? styles.highlight : ""}>↓ Download CSV</span></div></>}
                  </div>}
                  <div className={styles.callout}><b>{step + 1}</b>{current.focus}</div>
                </div>
              </div>
              <div className={styles.caption} aria-live="polite"><span className={styles.eyebrow}>STEP {step + 1} OF {steps.length}</span><h3>{current.title}</h3><p>{current.text}</p></div>
              <p className={styles.note}>Illustrative screens. Follow these steps in your broker’s website; its appearance may vary.</p>
            </div>
          </div>
        </div>
        <footer className={styles.footer}><span>{broker?.name} · {step + 1} / {steps.length}</span><div><Button variant="ghost" disabled={step === 0} onClick={() => setStep((value) => value - 1)}>Back</Button>{step === steps.length - 1 ? <Button onClick={() => setOpen(false)}>Ready to upload</Button> : <Button onClick={() => setStep((value) => value + 1)}>Next step →</Button>}</div></footer>
      </div>
    </div>, document.body)}
  </>;
}
