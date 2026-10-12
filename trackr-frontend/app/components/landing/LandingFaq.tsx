"use client";

import { useState } from "react";
import { Reveal } from "./Reveal";
import { Chevron } from "./icons";

const FAQS = [
  {
    q: "Do you need my broker login?",
    a: "No, and there is nowhere to put one. No passwords, no API keys, no OTPs. You upload a file or forward an email, and that is the whole setup.",
  },
  {
    q: "What happens to my cost after a bonus or split?",
    a: "Your cost stays the same and your holding period is not reset. We check the action against your own trades first. If the numbers do not add up, we hold it for you to review instead of guessing.",
  },
  {
    q: "What can I track here?",
    a: "Indian listed shares on NSE and BSE. One thing, done properly. Mutual funds, bonds and the rest are not covered yet.",
  },
  {
    q: "Is Trackr+ a broker or an adviser?",
    a: "Neither. We do not place orders, hold your money, or tell you what to buy. We show you what your own trades add up to.",
  },
  {
    q: "Can I get my data out again?",
    a: "Yes. You can export your full trade history at any time. Delete really deletes it, it does not just hide it.",
  },
];

export function LandingFaq() {
  const [open, setOpen] = useState(0);

  return (
    <section id="faq" className="l-section">
      <div className="l-container l-split">
        <Reveal className="l-split-text">
          <h2 className="l-h2" style={{ marginTop: 0 }}>Common questions.</h2>
          <p style={{ margin: "16px 0 0", fontSize: 15, lineHeight: 1.6, color: "var(--l-text-3)" }}>
            Still not sure about something? <a href="#contact" style={{ color: "var(--l-accent)" }}>Talk to us</a>. A person replies.
          </p>
        </Reveal>

        <Reveal className="l-split-rest" delay={80}>
          {FAQS.map((item, i) => {
            const expanded = open === i;
            return (
              <div key={item.q} className="l-faq-row">
                <button
                  type="button"
                  className="l-faq-btn"
                  aria-expanded={expanded}
                  aria-controls={`l-faq-a-${i}`}
                  onClick={() => setOpen(expanded ? -1 : i)}
                >
                  {item.q}
                  <Chevron className="l-faq-chev" />
                </button>
                {expanded && (
                  <p id={`l-faq-a-${i}`} className="l-faq-answer">{item.a}</p>
                )}
              </div>
            );
          })}
        </Reveal>
      </div>
    </section>
  );
}
