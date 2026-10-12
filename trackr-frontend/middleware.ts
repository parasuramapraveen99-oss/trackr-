import { NextRequest, NextResponse } from "next/server";
import { contentSecurityPolicy } from "./lib/csp";

export function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = contentSecurityPolicy(nonce, process.env.NODE_ENV === "development");
  const requestHeaders = new Headers(request.headers);
  // Overwrite supplied values. Next reads this CSP to nonce its own scripts.
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

export const config = {
  matcher: ["/((?!api/|_next/static|_next/image|favicon.ico|robots.txt|sitemap.xml).*)"],
};
