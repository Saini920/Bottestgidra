/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Static export — every page is a client component, so the whole app
  // becomes plain HTML/JS/CSS in `out/`. This deploys perfectly on
  // Cloudflare Pages (no server needed on the frontend side).
  output: "export",
};

module.exports = nextConfig;
