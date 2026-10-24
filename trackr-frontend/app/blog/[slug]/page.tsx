import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { blogCanonical, getBlogPost, getBlogPosts } from "@/lib/blog";

export function generateStaticParams() {
  return getBlogPosts().map(({ slug }) => ({ slug }));
}

export function generateMetadata({ params }: { params: { slug: string } }): Metadata {
  const post = getBlogPost(params.slug);
  if (!post) return { title: "Article not found · Trackr+", robots: { index: false } };
  return { title: `${post.title} · Trackr+`, description: post.description,
    alternates: { canonical: blogCanonical(`/blog/${post.slug}`) },
    openGraph: { type: "article", title: post.title, description: post.description,
      publishedTime: post.date, modifiedTime: post.updated } };
}

export default function Article({ params }: { params: { slug: string } }) {
  const post = getBlogPost(params.slug);
  if (!post) notFound();
  const related = getBlogPosts().filter((item) => item.slug !== post.slug)
    .sort((a, b) => Number(b.tags.some((tag) => post.tags.includes(tag))) - Number(a.tags.some((tag) => post.tags.includes(tag)))).slice(0, 3);
  return <>
    <article className="blog-article">
      <Link href="/blog" className="blog-back">← All articles</Link>
      <header><p className="blog-eyebrow">{post.tags.join(" / ")}</p><h1>{post.title}</h1><p className="blog-deck">{post.description}</p>
        <div className="blog-byline">Trackr+ team <span>·</span> Updated <time dateTime={post.updated}>{post.updated}</time></div>
      </header>
      <div className="blog-prose"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
        a: ({ href, children }) => href?.startsWith("/")
          ? <Link href={href}>{children}</Link>
          : <a href={href} rel="noopener noreferrer">{children}</a>,
        table: ({ children }) => <div className="blog-table"><table>{children}</table></div>,
      }}>{post.content}</Markdown></div>
      <aside className="blog-cta"><h2>See the records behind your returns.</h2><p>Bring your broker records into Trackr+ and explore your portfolio.</p><Link href="/register" className="blog-button">Get started</Link></aside>
    </article>
    <section className="blog-related" aria-label="Related articles"><h2>Keep reading</h2><div className="blog-grid">{related.map((item) => <article className="blog-card" key={item.slug}><p className="blog-eyebrow">{item.tags[0]}</p><h3><Link href={`/blog/${item.slug}`}>{item.title}</Link></h3><p>{item.description}</p></article>)}</div></section>
  </>;
}
