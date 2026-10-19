"use client";

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Icon } from "@trackr/ui";
import styles from "./GmailForwardingGuide.module.css";

type Screen = "inbox" | "quick" | "general" | "forwarding" | "add" | "confirm" | "sent" | "trackr" | "verify" | "filters" | "criteria" | "actions" | "saved";
type Scene = { id: string; screen: Screen; target: string; title: string; caption: string; route: string[]; zoom: number };
const FORWARDING = ["Gmail", "Settings", "Forwarding and POP/IMAP"];
const FILTERS = ["Gmail", "Settings", "Filters and Blocked Addresses"];
const CRITERIA = [...FILTERS, "Create a new filter"];
const ACTIONS = [...CRITERIA, "Choose actions"];

// Every screen transition has its own navigation action. The guide never jumps
// directly from forwarding settings to a filter dialog without showing the way there.
const SCENES: Scene[] = [
  { id: "settings", screen: "inbox", target: "settings", title: "Click the Gmail settings gear", caption: "Start in your Gmail inbox on a computer. Click the gear in the top-right corner.", route: ["Gmail", "Inbox"], zoom: 1.45 },
  { id: "all-settings", screen: "quick", target: "see-all", title: "Click See all settings", caption: "The gear opens Quick settings on the right. Click See all settings at the top of that panel.", route: ["Gmail", "Quick settings"], zoom: 1.3 },
  { id: "forwarding-tab", screen: "general", target: "forwarding-tab", title: "Open Forwarding and POP/IMAP", caption: "In the Settings tab row, click Forwarding and POP/IMAP. Some Gmail accounts call this tab Forwarding.", route: ["Gmail", "Settings", "General"], zoom: 1.2 },
  { id: "add-address", screen: "forwarding", target: "add-address", title: "Click Add a forwarding address", caption: "At the top of the Forwarding section, click Add a forwarding address. This opens the address dialog shown next.", route: FORWARDING, zoom: 1.3 },
  { id: "paste-address", screen: "add", target: "address-input", title: "Paste your Trackr address", caption: "Copy your real forwarding address from the Trackr panel beside this guide. Paste it into this Gmail field.", route: [...FORWARDING, "Add a forwarding address"], zoom: 1.2 },
  { id: "next", screen: "add", target: "next", title: "Click Next in the address dialog", caption: "With your Trackr address entered, click the blue Next button at the bottom-right of the same dialog.", route: [...FORWARDING, "Add a forwarding address"], zoom: 1.2 },
  { id: "proceed", screen: "confirm", target: "proceed", title: "Click Proceed in the new window", caption: "Next opens Gmail’s confirmation window. Check the forwarding address, then click Proceed. Allow Gmail’s popup if your browser blocks it.", route: ["Gmail", "Confirm forwarding address", "New window"], zoom: 1.2 },
  { id: "sent", screen: "sent", target: "ok", title: "Click OK after Gmail sends the code", caption: "Back in Gmail, this message confirms that the verification code was sent to Trackr. Click OK.", route: [...FORWARDING, "Code sent"], zoom: 1.2 },
  { id: "copy-code", screen: "trackr", target: "copy-code", title: "Return to Trackr and copy the code", caption: "Switch to your Trackr browser tab. In the Gmail forwarding panel, copy the confirmation code when it arrives.", route: ["Trackr+", "Gmail forwarding", "Confirmation code"], zoom: 1.15 },
  { id: "paste-code", screen: "verify", target: "verify-code", title: "Return to Gmail and paste the code", caption: "Switch back to Gmail → Settings → Forwarding and POP/IMAP. Paste the code into the confirmation-code field beside your address.", route: FORWARDING, zoom: 1.25 },
  { id: "verify", screen: "verify", target: "verify-button", title: "Click Verify", caption: "Click Verify beside the code field. Your Trackr address is now available for Gmail forwarding filters.", route: FORWARDING, zoom: 1.25 },
  { id: "global-off", screen: "forwarding", target: "global-off", title: "Keep forwarding of all mail off", caption: "Keep Disable forwarding selected here. This controls ALL incoming mail; it does not remove or unverify your Trackr address. We will forward only matching statements with a filter.", route: FORWARDING, zoom: 1.25 },
  { id: "save-forwarding", screen: "forwarding", target: "save-settings", title: "Save this forwarding setting", caption: "If you changed the all-mail setting, click Save Changes at the bottom. Gmail returns to the inbox. If it was already off, return to Inbox yourself.", route: [...FORWARDING, "Save Changes"], zoom: 1.15 },
  { id: "settings-again", screen: "inbox", target: "settings", title: "From Inbox, open Settings again", caption: "Now we will create the attachment filter. From your inbox, click the same settings gear at the top right.", route: ["Gmail", "Inbox"], zoom: 1.45 },
  { id: "all-settings-again", screen: "quick", target: "see-all", title: "Click See all settings again", caption: "In the Quick settings panel on the right, click See all settings to return to Gmail’s settings tabs.", route: ["Gmail", "Quick settings"], zoom: 1.3 },
  { id: "filters-tab", screen: "general", target: "filters-tab", title: "Open Filters and Blocked Addresses", caption: "This time choose Filters and Blocked Addresses in the Settings tab row. It is next to the Forwarding tab.", route: ["Gmail", "Settings", "General"], zoom: 1.2 },
  { id: "new-filter", screen: "filters", target: "new-filter", title: "Click Create a new filter", caption: "Inside Filters and Blocked Addresses, click Create a new filter below the filter list. This opens the search-criteria dialog.", route: FILTERS, zoom: 1.2 },
  { id: "sender", screen: "criteria", target: "sender", title: "Enter your statement sender", caption: "In From, enter the exact sender address from one of your broker or CAS emails. The address in this demo is only an example.", route: CRITERIA, zoom: 1.2 },
  { id: "attachment", screen: "criteria", target: "attachment", title: "Tick Has attachment", caption: "Keep the From address and tick Has attachment in the same dialog. Both conditions must match for an email to be forwarded.", route: CRITERIA, zoom: 1.25 },
  { id: "criteria-next", screen: "criteria", target: "criteria-next", title: "Click Create filter to choose actions", caption: "Click Create filter at the bottom of this criteria dialog. It opens a second screen where you choose what Gmail does with matching mail.", route: CRITERIA, zoom: 1.2 },
  { id: "forward-action", screen: "actions", target: "forward-action", title: "Tick Forward it to", caption: "On the new action screen, tick Forward it to. Leave Delete it unchecked so the original message stays in Gmail.", route: ACTIONS, zoom: 1.2 },
  { id: "open-addresses", screen: "actions", target: "address-select", title: "Open the forwarding-address dropdown", caption: "Beside Forward it to, click Choose an address. This dropdown contains the addresses you have already verified in Gmail.", route: ACTIONS, zoom: 1.2 },
  { id: "choose-address", screen: "actions", target: "address-option", title: "Select your verified Trackr inbox", caption: "In the opened dropdown, select the same Trackr address you added and verified earlier. If it is missing, finish verification in the Forwarding tab first.", route: ACTIONS, zoom: 1.2 },
  { id: "save-filter", screen: "actions", target: "save-filter", title: "Click Create filter to save the rule", caption: "With Forward it to checked and your Trackr address selected, click the blue Create filter button. This saves the forwarding rule.", route: ACTIONS, zoom: 1.2 },
  { id: "saved", screen: "saved", target: "saved-rule", title: "Find your saved forwarding filter", caption: "Gmail lists the saved rule under Filters and Blocked Addresses. It forwards new mail from this sender only when it has an attachment. Repeat for each statement sender; upload older files separately.", route: FILTERS, zoom: 1.05 },
];
const CHAPTERS = [
  { start: 0, label: "Gmail settings" }, { start: 3, label: "Add address" },
  { start: 8, label: "Verify address" }, { start: 13, label: "Find Filters tab" },
  { start: 17, label: "Match attachments" }, { start: 20, label: "Forward & save" },
];
const SCENE_MS = 7500;

const SAMPLE_ADDRESS = "mail-sample@portfolio.example";
const SAMPLE_CODE = "123456789";

export function GmailForwardingGuide() {
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(true);
  const [speed, setSpeed] = useState(1);
  const player = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<HTMLDivElement>(null);
  const [target, setTarget] = useState<{ id: string; box: number[] } | null>(null);
  const scene = SCENES[step]!;

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => {
      if (step === SCENES.length - 1) setPlaying(false);
      else setStep((value) => value + 1);
    }, SCENE_MS / speed);
    return () => window.clearTimeout(timer);
  }, [playing, step, speed]);

  useEffect(() => {
    const pause = () => { if (document.hidden) setPlaying(false); };
    document.addEventListener("visibilitychange", pause);
    return () => document.removeEventListener("visibilitychange", pause);
  }, []);

  // Named targets keep the cursor on the actual control rather than an estimated
  // coordinate or a positional selector that can drift when the screen changes.
  useEffect(() => {
    const node = cameraRef.current;
    const element = node?.querySelector<HTMLElement>(`[data-tour="${scene.target}"]`);
    if (!node || !element) return;
    function measure() {
      const frame = node!.getBoundingClientRect(); const rect = element!.getBoundingClientRect();
      if (!frame.width || !frame.height) return;
      const padding = .6;
      setTarget({ id: scene.id, box: [(rect.left - frame.left) / frame.width * 100 - padding, (rect.top - frame.top) / frame.height * 100 - padding, rect.width / frame.width * 100 + padding * 2, rect.height / frame.height * 100 + padding * 2] });
    }
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node); observer.observe(element);
    return () => observer.disconnect();
  }, [scene]);

  function goTo(value: number) { setStep(value); setPlaying(false); }
  const focus = target?.id === scene.id ? target.box : [40, 40, 20, 10];
  const origin = `${Math.min(95, Math.max(5, focus[0]! + focus[2]! / 2))}% ${Math.min(90, Math.max(10, focus[1]! + focus[3]! / 2))}%`;
  const camera = { "--camera-zoom": zoom ? scene.zoom : 1, "--camera-origin": origin, "--mobile-zoom": zoom ? 1.5 : 1 } as CSSProperties;

  return (
    <section className={styles.guide} aria-label="Animated Gmail forwarding tutorial">
      <div className={styles.guideHeader}><span className={styles.liveDot} /> GMAIL WALKTHROUGH <span>25 steps · About 3 minutes</span></div>
      <div className={styles.player} ref={player}>
        <div className={styles.browserBar}><span /><span /><span /><div><Icon name="privacy" size={10} />{scene.screen === "trackr" ? "Trackr+" : "mail.google.com"}</div><small>DEMO</small></div>
        <div className={styles.timeline} aria-label="Choose a tutorial step">
          {SCENES.map((item, i) => <button key={item.id} type="button" title={`${i + 1}. ${item.title}`} aria-label={`Step ${i + 1}: ${item.title}`} aria-current={step === i ? "step" : undefined} onClick={() => goTo(i)} className={i < step ? styles.watched : ""}><span key={`${step}-${speed}`} style={i === step ? { animation: playing ? `${styles.progress} ${SCENE_MS / speed}ms linear forwards` : "none", width: playing ? undefined : "45%" } : undefined} /></button>)}
        </div>
        <div className={styles.controls}>
          <button type="button" className={styles.play} aria-label={playing ? "Pause tutorial" : "Play tutorial"} onClick={() => { if (step === SCENES.length - 1) setStep(0); setPlaying((value) => !value); }}>{playing ? "Ⅱ" : "▶"}</button>
          <button type="button" aria-label="Previous tutorial step" disabled={step === 0} onClick={() => goTo(step - 1)}><Icon name="chevron-left" size={16} /></button>
          <select className={styles.stepSelect} aria-label="Tutorial step" value={step} onChange={(event) => goTo(Number(event.target.value))}>{SCENES.map((item, i) => <option key={item.id} value={i}>{i + 1} / {SCENES.length} · {item.title}</option>)}</select>
          <button type="button" aria-label="Next tutorial step" disabled={step === SCENES.length - 1} onClick={() => goTo(step + 1)}><Icon name="chevron-right" size={16} /></button>
          <div className={styles.controlSpacer} />
          <button type="button" aria-label="Replay tutorial" onClick={() => { setStep(0); setPlaying(true); }}><Icon name="refresh" size={15} /></button>
          <button type="button" aria-label={`Playback speed ${speed} times`} onClick={() => setSpeed((value) => value === 1 ? 1.5 : value === 1.5 ? .75 : 1)}>{speed}×</button>
          <button type="button" aria-label="Toggle tutorial zoom" aria-pressed={zoom} onClick={() => setZoom((value) => !value)}><Icon name="search" size={14} /><span>Zoom</span></button>
          <button type="button" aria-label="View tutorial fullscreen" onClick={() => { if (document.fullscreenElement) void document.exitFullscreen().catch(() => {}); else void player.current?.requestFullscreen?.().catch(() => {}); }}>⛶</button>
        </div>
        <div className={styles.caption} aria-live="polite" aria-atomic="true"><span>{String(step + 1).padStart(2, "0")}</span><div><strong>{scene.title}</strong><p>{scene.caption}</p></div></div>
        <div className={styles.route} aria-label="Current screen location">{scene.route.map((part, i) => <span key={`${i}-${part}`}>{i > 0 && <b aria-hidden="true">›</b>}{part}</span>)}</div>
        <div className={styles.viewport} style={camera}>
          <div ref={cameraRef} key={scene.id} className={`${styles.camera} ${zoom ? styles.zoomAnimation : ""}`}>
            <GmailScene scene={scene} address={SAMPLE_ADDRESS} />
            <div key={scene.id} className={styles.spotlight} style={{ left: `${focus[0]}%`, top: `${focus[1]}%`, width: `${focus[2]}%`, height: `${focus[3]}%` }}>
              <span className={styles.cursor} style={{ animationPlayState: playing ? "running" : "paused" }}><svg width="22" height="28" viewBox="0 0 22 28"><path d="M2 2v21l5-6 4 9 4-2-4-9 8-1Z" fill="#173f36" stroke="white" strokeWidth="1.5" /></svg></span>
            </div>
          </div>
        </div>
      </div>
      <nav className={styles.chapters} aria-label="Tutorial chapters">{CHAPTERS.map((chapter, i) => <button key={chapter.start} type="button" aria-current={step >= chapter.start && step < (CHAPTERS[i + 1]?.start ?? SCENES.length) ? "step" : undefined} onClick={() => goTo(chapter.start)}><span>{i + 1}</span>{chapter.label}</button>)}</nav>
      <p className={styles.demoNote}>Sample address and code shown for illustration. Use your own address and verification code from the setup panel. <a href="https://support.google.com/mail/answer/10957?hl=en" target="_blank" rel="noreferrer">Gmail help ↗</a></p>
    </section>
  );
}

function Check({ checked = false, tour, children }: { checked?: boolean; tour?: string; children: ReactNode }) {
  return <div className={styles.checkRow}><span data-tour={tour} className={checked ? styles.checked : styles.checkbox}>{checked ? "✓" : ""}</span>{children}</div>;
}
function FakeButton({ children, blue = false, tour }: { children: ReactNode; blue?: boolean; tour?: string }) { return <span data-tour={tour} className={blue ? styles.googleBlue : styles.fakeButton}>{children}</span>; }

function GmailScene({ scene, address }: { scene: Scene; address: string }) {
  const { screen, id } = scene;
  const inbox = screen === "inbox" || screen === "quick";
  const filters = screen === "filters" || screen === "criteria" || screen === "actions" || screen === "saved";
  const verified = id === "global-off" || id === "save-forwarding";
  const modal = screen === "add" || screen === "confirm" || screen === "sent";
  const checked = id !== "forward-action";
  const chosen = id === "save-filter";

  if (screen === "trackr") return <div className={`${styles.gmail} ${styles.trackrDemo}`} aria-hidden="true">
    <header>Trackr+ <span>Gmail forwarding</span></header>
    <div className={styles.trackrCodeCard}><h3>Gmail verification</h3><p>Gmail’s code appears in this Trackr panel.</p><div><code>{SAMPLE_CODE}</code><FakeButton tour="copy-code">Copy</FakeButton></div><p>Copy the real code, then switch back to your Gmail tab.</p></div>
  </div>;

  return <div className={styles.gmail} aria-hidden="true">
    <div className={styles.gmailTop}><span className={styles.gmailBrand}><b>M</b> Gmail</span><div className={styles.search}>⌕　Search mail <Icon name="filter" size={16} /></div><span>?</span><span className={styles.gear} data-tour="settings"><Icon name="settings" size={22} /></span><span className={styles.avatar}>T</span></div>
    <aside className={styles.gmailSidebar}><div>＋ Compose</div><b>Inbox <small>12</small></b><span>☆ Starred</span><span>◷ Snoozed</span><span>➤ Sent</span><span>▱ Drafts</span></aside>
    <main className={styles.gmailMain}>
      {inbox ? <><div className={styles.inboxToolbar}>□　↻　⋮</div><div className={styles.inboxTabs}><b>Primary</b><span>Promotions</span><span>Social</span></div>{Array.from({ length: 7 }, (_, i) => <div key={i} className={styles.emailRow}><span>□　☆</span><b>{i === 0 ? "Your broker" : ["Updates", "Newsletter", "Your bank"][i % 3]}</b><div>{i === 0 ? "Contract note / CAS statement" : "Your inbox message"}{i === 0 && <small>▱ statement.pdf</small>}</div><time>9:30</time></div>)}</> : <>
        <h3>Settings</h3>
        <div className={styles.settingsTabs}>
          <span className={screen === "general" ? styles.activeTab : ""}>General</span><span>Labels</span><span>Inbox</span><span>Accounts</span>
          <span data-tour="filters-tab" className={filters ? styles.activeTab : ""}>Filters and Blocked Addresses</span>
          <span data-tour="forwarding-tab" className={screen !== "general" && !filters ? styles.activeTab : ""}>Forwarding and POP/IMAP</span>
        </div>
        {filters ? <div className={styles.filtersPage}>
          <p>The following filters are applied to all incoming mail:</p>
          {screen === "saved" ? <div data-tour="saved-rule" className={styles.savedRule}><span>□</span><div><b>Matches:</b> from: statements@your-broker.example has:attachment<br /><b>Do this:</b> Forward to {address}</div><span>Edit　Delete</span></div> : <div className={styles.emptyFilters}>Your saved filters appear here.</div>}
          <div className={styles.filterLinks}><span data-tour="new-filter">Create a new filter</span><span>Import filters</span></div>
        </div> : screen === "general" ? <div className={styles.generalSettings}><p><b>Language:</b>　Gmail display language: English</p><p><b>Default text style:</b>　Sans Serif　　B　I　U</p><p><b>Images:</b>　◉ Always display external images</p><p><b>Undo Send:</b>　Send cancellation period: 5 seconds</p><p><b>Default reply behavior:</b>　◉ Reply</p></div> : <>
          <div className={styles.settingRow}><b>Forwarding:</b><div>
            {verified && <><p data-tour="global-off"><span className={styles.radio}>◉</span> Disable forwarding</p><p>○ Forward a copy of incoming mail to <span className={styles.fakeInput}>{address}</span></p></>}
            <FakeButton tour="add-address">Add a forwarding address</FakeButton>
            {screen === "verify" && <div className={styles.verify}><span>Verify {address}</span><div className={styles.codeEntry}><span data-tour="verify-code" className={styles.fakeInput}>{SAMPLE_CODE}</span><FakeButton tour="verify-button">Verify</FakeButton></div></div>}
            {verified && <p className={styles.verifiedAddress}>✓ {address} · Verified for filters</p>}
            <p className={styles.tip}>Tip: You can forward only some of your mail by creating a filter.</p>
          </div></div>
          <div className={styles.settingRow}><b>POP download:</b><div><p><strong>Status: POP is disabled</strong></p><p>○ Enable POP for all mail</p><p>○ Enable POP for mail that arrives from now on</p></div></div>
          <div className={styles.settingRow}><b>IMAP access:</b><div><p>Status: IMAP is enabled</p></div></div>
          <div className={styles.saveChanges}><FakeButton tour="save-settings">Save Changes</FakeButton></div>
        </>}
      </>}
    </main>
    {screen === "quick" && <aside className={styles.quickSettings}><h3>Quick settings　×</h3><div className={styles.seeAll} data-tour="see-all">See all settings</div><h4>Density</h4><p>◉ Default</p><p>○ Comfortable</p><p>○ Compact</p><hr /><h4>Theme</h4><div className={styles.themeBlocks}><span /><span /><span /></div></aside>}
    {modal && <div className={styles.dialogScrim}><div className={styles.googleDialog}>
      {screen === "confirm" && <div className={styles.popupTitle}>Confirm forwarding address — Gmail　×</div>}
      <h3>{screen === "confirm" ? "Confirm forwarding address" : "Add a forwarding address"}</h3>
      {screen === "add" ? <><p>Please enter a new forwarding email address:</p><div data-tour="address-input" className={styles.addressInput}>{address}</div><footer>Cancel <FakeButton blue tour="next">Next</FakeButton></footer></> : screen === "confirm" ? <><p>Forwarding mail to</p><div className={styles.addressInput}>{address}</div><footer><FakeButton tour="proceed">Proceed</FakeButton><FakeButton>Cancel</FakeButton></footer></> : <><p>A confirmation code has been sent to your Trackr inbox to verify permission.</p><footer><FakeButton blue tour="ok">OK</FakeButton></footer></>}
    </div></div>}
    {screen === "criteria" && <div className={styles.filterDialog}>
      <div className={styles.example}>EXAMPLE SENDER · USE YOUR OWN</div>
      {["From", "To", "Subject", "Has the words", "Doesn’t have"].map((label) => <div className={styles.filterField} key={label}><span>{label}</span><div data-tour={label === "From" ? "sender" : undefined}>{label === "From" ? "statements@your-broker.example" : ""}</div></div>)}
      <div className={styles.filterSize}>Size　greater than　 ─────　MB</div>
      <Check checked={id !== "sender"} tour="attachment">Has attachment</Check>
      <footer><span data-tour="criteria-next">Create filter</span><FakeButton blue>Search</FakeButton></footer>
    </div>}
    {screen === "actions" && <div className={styles.filterDialog}>
      <p>When a message arrives that matches this search:</p>
      <div className={styles.filterSummary}>from: statements@your-broker.example　has:attachment</div>
      <Check>Skip the Inbox (Archive it)</Check><Check>Mark as read</Check><Check>Star it</Check><Check>Apply the label:　Choose label…</Check>
      <Check checked={checked} tour="forward-action">Forward it to: <span className={styles.addressDropdown}>
        <span data-tour="address-select" className={styles.fakeInput}>{chosen ? address : "Choose an address"} ▾</span>
        {id === "choose-address" && <span className={styles.dropdownOptions}><span data-tour="address-option">{address}</span></span>}
      </span></Check>
      <Check>Delete it</Check><Check>Never send it to Spam</Check>
      <p className={styles.tip}>Old mail will not be forwarded.</p>
      <footer><FakeButton blue tour="save-filter">Create filter</FakeButton></footer>
    </div>}
  </div>;
}
