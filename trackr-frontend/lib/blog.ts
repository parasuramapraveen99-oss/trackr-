import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";

export interface BlogPost {
  slug: string;
  title: string;
  description: string;
  date: string;
  updated: string;
  tags: string[];
  content: string;
}

const directory = path.join(process.cwd(), "content", "blog");
const validSlug = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function isoDate(value: unknown): string {
  const text = value instanceof Date ? value.toISOString().slice(0, 10) : String(value ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text) || Number.isNaN(Date.parse(text))) {
    throw new Error("A blog post has an invalid date.");
  }
  return text;
}

/** Only checked-in, explicitly published articles can be served. No request path is read. */
export function getBlogPosts(): BlogPost[] {
  return fs.readdirSync(directory).filter((file) => file.endsWith(".md")).flatMap((file) => {
    const slug = file.slice(0, -3);
    if (!validSlug.test(slug)) throw new Error(`Invalid blog filename: ${file}`);
    const { data, content } = matter(fs.readFileSync(path.join(directory, file), "utf8"));
    if (data.published !== true) return [];
    if (data.slug !== slug || typeof data.title !== "string" || typeof data.description !== "string") {
      throw new Error(`Incomplete blog metadata: ${file}`);
    }
    return [{
      slug, title: data.title, description: data.description,
      date: isoDate(data.date), updated: isoDate(data.updated ?? data.date),
      tags: Array.isArray(data.tags) ? data.tags.map(String) : [],
      content: content.replace(/^\s*#\s+[^\n]+\n/, "").trim(),
    }];
  }).sort((a, b) => b.date.localeCompare(a.date) || a.title.localeCompare(b.title));
}

export function getBlogPost(slug: string): BlogPost | undefined {
  return validSlug.test(slug) ? getBlogPosts().find((post) => post.slug === slug) : undefined;
}

/** Canonicals are emitted only when a real public origin has been configured. */
export function blogCanonical(pathname: string): string | undefined {
  const configured = process.env.NEXT_PUBLIC_SITE_URL;
  if (!configured) return undefined;
  const url = new URL(configured);
  if (url.protocol !== "https:" || url.username || url.password) {
    throw new Error("NEXT_PUBLIC_SITE_URL must be a public HTTPS origin without credentials.");
  }
  return new URL(pathname, url.origin).toString();
}
