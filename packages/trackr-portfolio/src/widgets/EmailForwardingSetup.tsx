"use client";

import { useEffect, useState } from "react";
import { api, useAuth } from "@trackr/core";
import type { AccountOut, ForwardingCodeOut } from "@trackr/core";
import { Badge } from "@trackr/ui";
import { CopyField } from "./ForwardingCopyField";
import { EmailAutomation } from "./EmailAutomation";
import { GmailForwardingGuide } from "./GmailForwardingGuide";
import styles from "./EmailForwardingSetup.module.css";

/** Gmail setup always shows the guide; only real server-supplied inboxes are copyable. */
export function EmailForwardingSetup({ account }: {
  account?: AccountOut;
  brokerHasEmailAdapter?: boolean;
}) {
  const { investorId: selectedInvestorId } = useAuth();
  const investorId = account?.investor_id ?? selectedInvestorId;
  const [inboxAddress, setInboxAddress] = useState<string | null>(null);
  const addresses = inboxAddress ? [inboxAddress] : account?.forwarding_addresses ?? [];
  const address = addresses[0];

  return (
    <div className={styles.experience}>
      <div className={styles.layout}>
        <aside className={styles.setup} aria-label="Your Gmail setup details">
          <div className={styles.intro}>
            <span className={styles.eyebrow}>Forwarding details</span>
            <h3>Your portfolio inbox</h3>
            <p>Copy your address when Gmail asks where to forward your statements.</p>
          </div>
          {investorId && <EmailAutomation investorId={investorId} onAddress={setInboxAddress} />}
          {addresses.map((value) => <div key={value} className={styles.address}>
            <span className={styles.eyebrow}>Your Trackr forwarding address</span>
            <CopyField value={value} label="statement forwarding address" />
          </div>)}
          {address && <div className={styles.address}>
            <span className={styles.eyebrow}>Gmail verification</span>
            <ForwardingCode investorId={investorId} showWaiting />
          </div>}
          <a className={styles.openGmail} href="https://mail.google.com/mail/u/0/#inbox" target="_blank" rel="noreferrer">Open Gmail ↗</a>
        </aside>
        <div className={styles.walkthrough}><GmailForwardingGuide address={address} /></div>
      </div>
    </div>
  );
}

/**
 * The code Gmail mailed to the forwarding address, once it arrives.
 *
 * POLLED, not pushed. The message lands on a webhook from the mail provider, so there is
 * nothing for the browser to subscribe to, and the wait is short and bounded -- Gmail
 * sends within seconds of the filter being created. A ten-second poll while this panel is
 * open is cheap and stops the moment the component unmounts.
 *
 * RENDERS NOTHING WHEN THERE IS NOTHING, deliberately. Almost every visit to this screen
 * is by someone who has not created a Gmail filter in the last day, and a permanent
 * "waiting for a code…" box would be a box that is wrong nearly all the time. It appears
 * when there is something true to say.
 */
export function ForwardingCode({ investorId, showWaiting = false }: { investorId: string; showWaiting?: boolean }) {
  const { token } = useAuth();
  const [codes, setCodes] = useState<ForwardingCodeOut[]>([]);

  useEffect(() => {
    if (!investorId || !token) return;
    let cancelled = false;

    const poll = () => {
      api
        .get<ForwardingCodeOut[]>(`/investors/${investorId}/forwarding-code`, { token })
        .then((rows) => {
          if (!cancelled) setCodes(rows);
        })
        .catch(() => {
          // A failed poll is not worth a message: the code either has not arrived or the
          // request blipped, and both look the same to the person waiting. The next tick
          // tries again.
        });
    };

    poll();
    const timer = setInterval(poll, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [investorId, token]);

  if (codes.length === 0) return showWaiting ? <p role="status" className={styles.codeHint}>Gmail’s confirmation code will appear here.</p> : null;

  return (
    <div style={{ display: "grid", gap: 8, marginBottom: 12 }}>
      {codes.map((c) => (
        <div key={`${c.broker}-${c.doctype}-${c.code}`} style={{ display: "grid", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span
              style={{
                font: "var(--type-label)",
                textTransform: "uppercase",
                letterSpacing: "var(--tracking-label)",
                color: "var(--text-tertiary)",
              }}
            >
              Confirmation code
            </span>
            <Badge tone="neutral" mono>
              {c.broker}
            </Badge>
          </div>
          <CopyField value={c.code} label="Gmail confirmation code" />
        </div>
      ))}
      <p className={styles.codeHint}>Copy this code into Gmail → Settings → Forwarding and POP/IMAP → Verify.</p>
    </div>
  );
}
