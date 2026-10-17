"use client";

import type { AccountOut } from "@/lib/api-types";
import { EmailForwardingSetup } from "../EmailForwardingSetup";

/** One investor inbox accepts supported statements across the investor's accounts. */
export function GmailForwardStep({ accounts }: { accounts: AccountOut[] }) {
  const account = accounts.find((item) => item.forwarding_addresses.length > 0) ?? accounts[0];
  return <EmailForwardingSetup account={account} />;
}
