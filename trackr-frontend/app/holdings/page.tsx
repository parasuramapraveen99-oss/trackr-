"use client";

/** `/holdings` -- see `app/page.tsx` for why this is a wrapper. */

import { HoldingsScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../components/portfolio-chrome";

export default function HoldingsPage() {
  return <HoldingsScreen chrome={PLUS_CHROME} />;
}
