"use client";

/**
 * `/holdings/[isin]` -- one security's lots, realised gains, dividends and ledger.
 *
 * The ISIN comes from the route, read inside the shared screen with `useParams`. Keyed by
 * ISIN and never by symbol: a ticker can be renamed and reused (CLAUDE.md rule 2).
 */

import { HoldingDetailScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../../components/portfolio-chrome";

export default function HoldingDetailPage() {
  return <HoldingDetailScreen chrome={PLUS_CHROME} />;
}
