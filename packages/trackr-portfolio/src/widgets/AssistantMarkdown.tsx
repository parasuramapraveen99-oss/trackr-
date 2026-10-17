import { Fragment, type ReactNode } from "react";
import styles from "./AssistantMarkdown.module.css";

/** Text-only Markdown: React escapes HTML; images and embedded links never load. */
function inline(text: string): ReactNode[] {
  const pattern = /(!?\[[^\]]+\]\([^)]*\)|\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*]+\*|_[^_]+_)/g;
  const result: ReactNode[] = [];
  let end = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index!;
    result.push(text.slice(end, start));
    const token = match[0];
    let element: ReactNode;
    if (token.startsWith("![")) element = null;
    else if (token.startsWith("[")) element = token.slice(1, token.indexOf("]("));
    else if (token.startsWith("**") || token.startsWith("__")) element = <strong>{token.slice(2, -2)}</strong>;
    else if (token.startsWith("`")) element = <code>{token.slice(1, -1)}</code>;
    else element = <em>{token.slice(1, -1)}</em>;
    result.push(<Fragment key={start}>{element}</Fragment>);
    end = start + token.length;
  }
  result.push(text.slice(end));
  return result;
}

const blockStart = /^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s?|```|---+$|\*\*\*+$)/;

export function AssistantMarkdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!.trim();
    const key = i;
    if (!line) { i++; continue; }
    if (/^```/.test(line)) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i]!.trim())) code.push(lines[i++]!);
      i++;
      blocks.push(<pre key={key}><code>{code.join("\n")}</code></pre>);
      continue;
    }
    const heading = /^(#{1,6})\s+(.+?)\s*#*$/.exec(line);
    if (heading) {
      blocks.push(<div key={key} role="heading" aria-level={heading[1]!.length} className={styles.heading}>{inline(heading[2]!)}</div>);
      i++;
      continue;
    }
    if (/^(?:---+|\*\*\*+)\s*$/.test(line)) { blocks.push(<hr key={key} />); i++; continue; }
    const ordered = /^\d+[.)]\s/.test(line);
    if (ordered || /^[-*+]\s/.test(line)) {
      const marker = ordered ? /^\d+[.)]\s+/ : /^[-*+]\s+/;
      const items: ReactNode[] = [];
      while (i < lines.length && marker.test(lines[i]!.trim())) {
        items.push(<li key={i}>{inline(lines[i++]!.trim().replace(marker, ""))}</li>);
      }
      blocks.push(ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>);
      continue;
    }
    if (/^>/.test(line)) {
      const quote: string[] = [];
      while (i < lines.length && /^>/.test(lines[i]!.trim())) quote.push(lines[i++]!.trim().replace(/^>\s?/, ""));
      blocks.push(<blockquote key={key}>{inline(quote.join(" "))}</blockquote>);
      continue;
    }
    const paragraph = [line];
    i++;
    while (i < lines.length && lines[i]!.trim() && !blockStart.test(lines[i]!.trim())) paragraph.push(lines[i++]!.trim());
    blocks.push(<p key={key}>{inline(paragraph.join(" "))}</p>);
  }
  return <div className={styles.markdown}>{blocks}</div>;
}
