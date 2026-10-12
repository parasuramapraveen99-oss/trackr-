"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

// Commented out 2026-09-08. Every figure this page showed -- both XIRR variants,
// absolute return, benchmark comparison -- already lives on the Dashboard
// (app/page.tsx), and this screen never got the ds/ redesign the rest of the app did,
// so it read as a different product. Nothing here is deleted: uncomment the block
// below (and the nav entry in ../components/nav.ts) to bring it back, whether as-is or
// redesigned.
//
// import { PerformanceTab } from "../components/tabs/PerformanceTab";
// import { PageHeader } from "../components/PageHeader";
// import { PageBody } from "../components/Shell";
//
// export default function PerformancePage() {
//   return (
//     <>
//       <PageHeader
//         title="Performance"
//         subtitle="XIRR, TWRR and realised gains, solved from the ledger's own cash flows."
//       />
//       <PageBody>
//         <PerformanceTab />
//       </PageBody>
//     </>
//   );
// }

/** Sends anyone who still has this URL saved to the Dashboard, where the same numbers
 *  live now.
 *
 *  THE QUERY STRING SURVIVES THE REDIRECT. This used to be a bare `router.replace("/")`,
 *  which threw it away -- so a saved `/performance?range=1y` landed on an unfiltered
 *  Dashboard, and the only interesting part of the bookmark was silently discarded. A
 *  compatibility redirect that loses the parameters is not compatibility.
 *
 *  Forwarded verbatim rather than translated: the Dashboard reads whichever parameters it
 *  understands and ignores the rest, which is a better contract than a hand-maintained
 *  mapping that would need editing every time either page grew a parameter. `/portfolio`
 *  does translate, because its `?tab=` named a route that no longer exists. */
export default function PerformancePage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  useEffect(() => {
    const query = searchParams.toString();
    router.replace(query ? `/?${query}` : "/");
  }, [router, searchParams]);
  return null;
}
