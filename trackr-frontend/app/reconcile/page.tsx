"use client";

/** `/reconcile` -- see `app/page.tsx` for why this is a wrapper. */

import { ReconcileScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../components/portfolio-chrome";

export default function ReconcilePage() {
  return <ReconcileScreen chrome={PLUS_CHROME} />;
}
