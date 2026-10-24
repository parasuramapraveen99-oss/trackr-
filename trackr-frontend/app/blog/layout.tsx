import Link from "next/link";
import "./blog.css";

export default function BlogLayout({ children }: { children: React.ReactNode }) {
  return <div className="blog-shell">
    <header className="blog-nav">
      <Link href="/" className="blog-brand">Trackr<span>+</span></Link>
      <nav aria-label="Blog navigation"><Link href="/blog">Journal</Link><Link href="/login">Sign in</Link><Link href="/register" className="blog-button">Get started</Link></nav>
    </header>
    <main id="blog-content">{children}</main>
    <footer className="blog-footer"><span>Trackr+ · Portfolio clarity from your records.</span>
      <nav aria-label="Footer"><Link href="/privacy-policy">Privacy</Link><Link href="/terms">Terms</Link><Link href="/grievance">Contact & grievance</Link></nav>
    </footer>
  </div>;
}
