"use client";

import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { api, useAuth } from "@trackr/core";
import type { AccountOut, ForwardingCodeOut } from "@trackr/core";
import { Badge, Banner, SegmentedControl } from "@trackr/ui";
import { CopyField } from "./ForwardingCopyField";

type Provider = "gmail" | "outlook" | "yahoo";

const PROVIDERS: { value: Provider; label: string }[] = [
  { value: "gmail", label: "Gmail" },
  { value: "outlook", label: "Outlook" },
  { value: "yahoo", label: "Yahoo" },
];

/** `{code}-{broker}-{doctype}@{domain}` (app.ingest.addresses.forwarding_addresses) --
 * the last hyphen-separated segment of the local part is the document type, used only
 * as a friendly label above each address. */
function doctypeLabel(address: string): string {
  const local = address.split("@")[0] ?? address;
  const parts = local.split("-");
  return parts[parts.length - 1] || local;
}

/**
 * Per-account, per-provider copy-paste walkthrough for docs/INGESTION.md's "Setup UX" --
 * the only client-side friction in the whole ingestion pipeline.
 *
 * ADDRESSES COME FROM THE ACCOUNT, and this component never constructs one. That is what
 * makes the empty case correct rather than blank: `forwarding_addresses` is empty both
 * for a broker with no email adapter and for a deployment with no mail-receiving domain,
 * and in either case the honest thing on screen is a sentence saying so — which this
 * component already did, and which is why the backend gate was written to reuse it.
 *
 * THE CONFIRMATION CODE IS CAUGHT AND SHOWN. Gmail will not forward anything until a code
 * it emails to the address is pasted back into Gmail's settings. That message has no
 * attachment and carries the code in its body, so while the webhook only ever looked at
 * attachments the code was silently dropped and nobody could finish Gmail's own setup --
 * the feature was a screen, not a feature (docs/ONBOARDING_FLOW.md, defect 2). The webhook
 * now recognises it and `ForwardingCode` below polls for it.
 *
 * WE SHOW IT RATHER THAN CLICKING IT. Gmail's mail also carries a confirmation link, and
 * following it server-side would work. It would also mean this product granting itself a
 * mail permission on the person's behalf, silently. Showing the code keeps the human in
 * the loop on a permission grant, and costs them one paste.
 */
export function EmailForwardingSetup({
  account,
  brokerHasEmailAdapter,
}: {
  account: AccountOut;
  /**
   * `BrokerOut.email_adapter` for this account's broker. Optional because two call sites
   * render this and only one of them has the registry to hand; when it is absent the
   * banner keeps the old both-causes wording rather than guessing at one.
   */
  brokerHasEmailAdapter?: boolean;
}) {
  const [provider, setProvider] = useState<Provider>("gmail");

  if (account.forwarding_addresses.length === 0) {
    // TWO CAUSES, AND THEY ARE NOT THE SAME NEWS. `forwarding_addresses` is empty both
    // for a broker nobody has written an email parser for and for a deployment with no
    // mail-receiving domain, and this banner used to hedge between them in one sentence
    // -- which named the user's broker while implying the broker was at fault, even for
    // Zerodha, whose adapter has existed all along. `BrokerOut.email_adapter` is the
    // domain-independent half of that AND, so it tells the two apart.
    return (
      <Banner tone="info">
        {brokerHasEmailAdapter === false ? (
          <>
            Email forwarding is not available for <strong>{account.broker}</strong> yet.
            You can upload files on the Import page.
          </>
        ) : brokerHasEmailAdapter === true ? (
          // The broker has a reader, so the empty list means this server receives no mail:
          // forwarding is switched off (EMAIL_FORWARDING_ENABLED) or has no domain.
          <>Email forwarding is turned off for now. Upload your files on the Import page.</>
        ) : (
          <>
            No forwarding address for <strong>{account.broker}</strong>. Upload your files on
            the Import page.
          </>
        )}
      </Banner>
    );
  }

  return (
    <div style={{ display: "grid", gap: 14 }}>
      {account.forwarding_addresses.map((address) => (
        <div key={address} style={{ display: "grid", gap: 4 }}>
          <p
            style={{
              margin: 0,
              font: "var(--type-label)",
              textTransform: "uppercase",
              letterSpacing: "var(--tracking-label)",
              color: "var(--text-tertiary)",
            }}
          >
            {doctypeLabel(address)} forwarding address
          </p>
          <CopyField value={address} label={`${account.broker} ${doctypeLabel(address)} address`} />
        </div>
      ))}

      <div style={{ display: "grid", gap: 10 }}>
        <SegmentedControl
          value={provider}
          onChange={(v) => setProvider(v as Provider)}
          options={PROVIDERS}
        />
        <div style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
          {provider === "gmail" && <ForwardingCode investorId={account.investor_id} />}

          {provider === "gmail" && (
            <Steps
              items={[
                <>Open any email from your broker in Gmail.</>,
                <>
                  Click the three dots and choose <strong>Filter messages like this</strong>.
                  Only your broker&apos;s emails will be forwarded.
                </>,
                <>
                  Tick <strong>Forward it to</strong> and pick the address above.
                </>,
                <>
                  Click <strong>Create filter</strong>.
                </>,
                <>
                  Gmail will send a code. We show it below. Paste it in Gmail under{" "}
                  <strong>Settings → Forwarding and POP/IMAP</strong> and click{" "}
                  <strong>Verify</strong>.
                </>,
              ]}
            />
          )}

          {provider === "outlook" && (
            <Steps
              items={[
                <>Open Settings → Mail → Rules.</>,
                <>
                  Add a rule with condition <strong>From</strong> your broker&apos;s sending
                  address (optionally also <strong>Subject includes</strong> &quot;Contract
                  Note&quot;).
                </>,
                <>
                  Set the action to <strong>Forward to</strong> and paste the address above.
                </>,
                <>Save the rule.</>,
              ]}
            />
          )}

          {provider === "yahoo" && (
            <div style={{ display: "grid", gap: 10 }}>
              <Banner tone="warning">
                Yahoo can&apos;t forward only your broker&apos;s emails. Use the steps below
                instead.
              </Banner>
              <Steps
                items={[
                  <>
                    In Yahoo, forward your mailbox to a Gmail account you control (Settings
                    → More Settings → Forwarding).
                  </>,
                  <>
                    In that Gmail account, follow the Gmail steps to forward your
                    broker&apos;s emails to the address above.
                  </>,
                ]}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Steps({ items }: { items: ReactNode[] }) {
  return (
    <ol style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 8 }}>
      {items.map((item, index) => (
        <li key={index} style={{ lineHeight: 1.55 } as CSSProperties}>
          {item}
        </li>
      ))}
    </ol>
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
export function ForwardingCode({ investorId }: { investorId: string }) {
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

  if (codes.length === 0) return null;

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
              Gmail confirmation code
            </span>
            <Badge tone="neutral" mono>
              {c.broker}
            </Badge>
          </div>
          <CopyField value={c.code} label="Gmail confirmation code" />
        </div>
      ))}
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        Paste this into Gmail under <strong>Settings → Forwarding and POP/IMAP</strong> and
        click <strong>Verify</strong>. Until you do, Gmail forwards nothing.
      </p>
    </div>
  );
}
