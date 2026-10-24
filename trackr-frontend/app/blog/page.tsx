import type { Metadata } from "next";
import Link from "next/link";
import { blogCanonical, getBlogPosts } from "@/lib/blog";

export const metadata: Metadata = {
  title: "Journal · Trackr+",
  description: "Plain-language guides to portfolio returns, dividends, records and investment tracking in India.",
  alternates: { canonical: blogCanonical("/blog") },
};

export default function BlogIndex() {
  const posts = getBlogPosts();
  return <>
    <section className="blog-intro"><p className="blog-eyebrow">THE TRACKR+ JOURNAL</p>
      <h1>Understand the numbers.<br /><span>Know your portfolio.</span></h1>
      <p>Clear explanations of returns, dividends and the records behind them. Built for investors who want to understand what they see.</p>
      <div className="blog-topics">Returns <span>·</span> Dividends <span>·</span> Portfolio records <span>·</span> Choosing a tracker</div>
    </section>
    <section aria-label="Articles" className="blog-grid">{posts.map((post, index) =>
      <article key={post.slug} className={`blog-card${index === 0 ? " blog-card-featured" : ""}`}>
        <div className="blog-card-meta"><span>{post.tags[0] ?? "Guide"}</span><time dateTime={post.updated}>{new Date(`${post.updated}T00:00:00Z`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })}</time></div>
        <h2><Link href={`/blog/${post.slug}`}>{post.title}</Link></h2><p>{post.description}</p>
        <Link href={`/blog/${post.slug}`} className="blog-read" aria-label={`Read ${post.title}`}>Read article <span aria-hidden="true">↗</span></Link>
      </article>
    )}</section>
  </>;
}
