"use client";

import Link from "next/link";
import "./landing.css";
import { Reveal } from "./Reveal";
import { LandingHeader } from "./LandingHeader";
import { LandingHero } from "./LandingHero";
import { LandingFaq } from "./LandingFaq";
import { LandingFooter } from "./LandingFooter";
import { ArrowRight, Doc, Mail, Shield, Table } from "./icons";

const INGESTION = [
  { icon: <Table />, title: "Upload a file", body: "Use the CSV or tradebook your broker already gives you. Upload the same file twice and nothing gets counted twice." },
  { icon: <Mail />, title: "Forward an email", body: "Forward your contract notes to us. We read the trades out of them for you." },
  { icon: <Doc />, title: "Share a PDF", body: "Send a contract note PDF. We turn it into a clean list of your trades." },
  { icon: <Shield />, title: "Delete anytime", body: "One click deletes everything. Your trades, your history, your account." },
];

export function Landing() {
  return (
    <div className="l-root">
      <LandingHeader />
      <LandingHero />

      <section id="security" className="l-section l-section-alt">
        <div className="l-container l-split">
          <Reveal className="l-split-text">
            <h2 className="l-h2" style={{ marginTop: 0 }}>We never ask for your broker password.</h2>
            <p className="l-body" style={{ marginTop: 18 }}>
              There is nowhere in Trackr+ to keep a broker password, an API key or an OTP.
              We could not store one if we tried. All we need is a file you upload or an
              email you forward.
            </p>
            <Link href="/coming-soon?feature=Compliance" className="l-navlink" style={{ marginTop: 22, display: "inline-flex", alignItems: "center", gap: 8, fontWeight: 500 }}>
              How we handle your data <ArrowRight size={15} />
            </Link>
          </Reveal>

          <div className="l-split-rest l-grid-2">
            {INGESTION.map((item, i) => (
              <Reveal key={item.title} delay={i * 70}>
                <div className="l-card" style={{ height: "100%" }}>
                  <span style={{ color: "var(--l-accent)", display: "block" }}>{item.icon}</span>
                  <span className="l-card-title" style={{ display: "block", marginTop: 12, fontSize: 15 }}>{item.title}</span>
                  <p className="l-card-body" style={{ marginTop: 6, fontSize: 12.5 }}>{item.body}</p>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      <LandingFaq />

      <LandingFooter />
    </div>
  );
}
