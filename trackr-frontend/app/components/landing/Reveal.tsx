"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Reveals its children once they scroll into view.
 *
 * The hidden state lives only inside `prefers-reduced-motion: no-preference` in
 * landing.css, so a viewer who has asked for no motion — or any browser without
 * IntersectionObserver, where this falls straight through to shown — sees the content
 * immediately rather than a blank band. The observer disconnects on first hit: this is
 * an entrance, not a scroll-linked effect, and re-animating on the way back up reads as
 * a bug.
 */
export function Reveal({
  children,
  delay = 0,
  className = "",
  style,
}: {
  children: React.ReactNode;
  delay?: number;
  className?: string;
  style?: React.CSSProperties;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setShown(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setShown(true);
          io.disconnect();
        }
      },
      { rootMargin: "0px 0px -12% 0px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={`l-reveal${shown ? " l-in" : ""}${className ? ` ${className}` : ""}`}
      style={{ transitionDelay: delay ? `${delay}ms` : undefined, ...style }}
    >
      {children}
    </div>
  );
}
