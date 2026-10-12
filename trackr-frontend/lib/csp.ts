/** Per-response script nonces; styles retain inline support for the existing UI. */
export function contentSecurityPolicy(nonce: string, development: boolean): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    `connect-src 'self'${development ? " ws: wss:" : ""}`,
    "object-src 'none'", "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'",
    ...(!development ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
}
