// Browser requests stay same-origin. Next consumes the upstream address server-side.
const isDev = process.env.NODE_ENV === "development";
const configuredApi = process.env.TRACKR_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL
  ?? (isDev ? "http://localhost:8000" : "");
if (!configuredApi) throw new Error("TRACKR_API_BASE_URL must be set for a production build.");
let upstream;
try { upstream = new URL(configuredApi); }
catch { throw new Error("API upstream must be a valid URL."); }
if (upstream.username || upstream.password || upstream.search || upstream.hash
    || !["http:", "https:"].includes(upstream.protocol)
    || (!isDev && upstream.protocol !== "https:")) {
  throw new Error("The production API upstream must use HTTPS and contain no credentials.");
}
const apiBase = configuredApi.replace(/\/$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  transpilePackages: ["@trackr/core", "@trackr/portfolio"],
  async rewrites() {
    return [{ source: "/api/trackr/:path*", destination: `${apiBase}/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
    ] }];
  },
};
export default nextConfig;
