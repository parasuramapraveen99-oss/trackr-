"use client";

import Link from "next/link";
import { BrandMark } from "./icons";

export function LandingHeader() {
  return (
    <header className="l-header">
      <div className="l-container l-header-inner">
        <a href="#top" className="l-brand l-rise">
          <span className="l-mark"><BrandMark /></span>
          <span className="l-wordmark">Trackr+</span>
        </a>

        <nav className="l-nav l-rise" style={{ animationDelay: "60ms" }}>
          <a href="#security" className="l-navlink">Security</a>
          <a href="#faq" className="l-navlink">FAQ</a>
          <Link href="/coming-soon?feature=Pricing" className="l-navlink">Pricing</Link>
        </nav>

        <div className="l-header-cta l-rise" style={{ animationDelay: "140ms" }}>
          <Link href="/login" className="l-navlink">Sign in</Link>
          <Link href="/register" className="l-btn l-btn-sm l-btn-solid">Get started</Link>
        </div>
      </div>
    </header>
  );
}
