"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

/**
 * Legacy route. Transactions, Import and Reconcile each have their own page now, so
 * this only exists to keep older links (and anyone's bookmarks) working — it forwards
 * `?tab=` to the route that replaced it rather than 404ing.
 */
const TAB_ROUTES: Record<string, string> = {
  transactions: "/transactions",
  import: "/import",
  reconcile: "/reconcile",
};

export default function PortfolioPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const tab = searchParams.get("tab") ?? "transactions";
  const isin = searchParams.get("isin");

  useEffect(() => {
    const base = TAB_ROUTES[tab] ?? "/transactions";
    router.replace(isin ? `${base}?isin=${encodeURIComponent(isin)}` : base);
  }, [tab, isin, router]);

  return null;
}
