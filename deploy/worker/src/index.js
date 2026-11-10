// jobcheck.tycodes.app -> Cloud Run. A Worker instead of a Cloud Run domain mapping because
// Cloud Run routes on the Host header (a plain proxied CNAME to *.run.app would 404), and the
// Worker lets Cloudflare Access + DNS + TLS all stay in Cloudflare.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const target = new URL(url.pathname + url.search, env.ORIGIN);
    // Forward method, headers (including Cf-Access-Jwt-Assertion and the CF_Authorization
    // cookie, which the app verifies) and body unchanged. fetch() sets Host from the target.
    const upstream = new Request(target, request);
    upstream.headers.set("X-Forwarded-Host", url.host);
    return fetch(upstream);
  },
};
