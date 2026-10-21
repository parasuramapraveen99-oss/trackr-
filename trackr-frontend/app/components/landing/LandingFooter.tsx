"use client";

import Link from "next/link";
import { Reveal } from "./Reveal";
import { BrandMark, Instagram, LinkedIn, XMark } from "./icons";

/**
 * Three columns, and no more.
 *
 * The closing statement and its "Get started" button are gone: the same call to action
 * sits in the header, fixed, the whole way down the page, and a second one three lines
 * above the copyright was competing with it rather than adding anything. The oversized
 * wordmark went with them — it was taller than every link in the footer put together.
 */
const COLUMNS = [
  {
    head: "Product",
    links: [
      { label: "Overview", href: "/#top" },
      { label: "How your data is handled", href: "/#security" },
      { label: "Pricing", href: "/coming-soon?feature=Pricing" },
    ],
  },
  {
    head: "Company",
    links: [
      { label: "About", href: "/coming-soon?feature=About" },
      { label: "Blog", href: "/coming-soon?feature=Blog" },
      { label: "Contact", href: "/coming-soon?feature=Contact" },
      { label: "Status", href: "/coming-soon?feature=Status" },
    ],
  },
  {
    head: "Legal",
    links: [
      { label: "Terms of Use", href: "/terms" },
      { label: "Privacy Policy", href: "/privacy-policy" },
      { label: "Grievance", href: "/grievance" },
      { label: "Data & DPDP", href: "/privacy" },
    ],
  },
];

export function LandingFooter() {
  return (
    <footer className="l-footer">
      <div className="l-container">
        <Reveal>
          <div className="l-footer-cols">
            <div className="l-footer-brand">
              <span className="l-brand">
                <span className="l-mark"><BrandMark /></span>
                <span className="l-wordmark">Trackr+</span>
              </span>
              <p style={{ margin: "14px 0 0", fontSize: 13.5, lineHeight: 1.6, color: "var(--l-text-3)" }}>
                Stock portfolio tracking for Indian investors. Every number comes from your
                own trades.
              </p>
              <div className="l-social">
                <a href="#linkedin" aria-label="Trackr+ on LinkedIn"><LinkedIn /></a>
                <a href="#x" aria-label="Trackr+ on X"><XMark /></a>
                <a href="#instagram" aria-label="Trackr+ on Instagram"><Instagram /></a>
              </div>
            </div>

            <div className="l-footer-links">
              {COLUMNS.map((col) => (
                <div key={col.head} className="l-footer-col">
                  <span className="l-footer-head">{col.head}</span>
                  {col.links.map((link) =>
                    link.href.startsWith("/") ? (
                      <Link key={link.label} href={link.href} className="l-flink">{link.label}</Link>
                    ) : (
                      <a key={link.label} href={link.href} className="l-flink">{link.label}</a>
                    )
                  )}
                </div>
              ))}
            </div>
          </div>
        </Reveal>

        <div className="l-footer-legal">
          <p>
            Trackr+ is a portfolio tracking product. It is not a broker, an investment
            adviser or an exchange. It does not give investment advice, place orders or
            hold your money. Indian listed shares only. Every figure shown on this page is
            sample data.
          </p>
          <span>© {new Date().getFullYear()} Trackr+</span>
        </div>
      </div>
    </footer>
  );
}
