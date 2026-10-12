"use client";

/** `/transactions` -- see `app/page.tsx` for why this is a wrapper. */

import { TransactionsScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../components/portfolio-chrome";

export default function TransactionsPage() {
  return <TransactionsScreen chrome={PLUS_CHROME} />;
}
