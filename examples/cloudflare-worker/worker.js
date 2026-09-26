// A tiny Cloudflare Worker + R2 upload host for spectacle-uploader.
//
//   PUT /upload?name=shot.png   (Authorization: Bearer <UPLOAD_TOKEN>)  ->  {"key": "AbC123xY.png"}
//   GET /f/<key>                                                        ->  the file (public)

const INLINE = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "video/mp4", "text/plain"]);
const TYPES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", mp4: "video/mp4", txt: "text/plain" };
const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
const KEY = /^[A-Za-z0-9]{8}(\.[a-z0-9]{1,8})?$/;

const text = (status, body) => new Response(body + "\n", { status, headers: { "content-type": "text/plain" } });

async function sameSecret(given, expected) {
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([given, expected].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))));
  return crypto.subtle.timingSafeEqual(a, b);
}

function newKey(ext) {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return Array.from(bytes, (n) => ALPHABET[n % ALPHABET.length]).join("") + (ext ? "." + ext : "");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "PUT" && url.pathname === "/upload") {
      const given = (request.headers.get("authorization") || "").replace(/^Bearer /, "");
      if (!env.UPLOAD_TOKEN || !(await sameSecret(given, env.UPLOAD_TOKEN))) return text(401, "no");
      const length = Number(request.headers.get("content-length"));
      const max = Number(env.MAX_MB || 90) * 1024 * 1024;
      if (!(length > 0 && length <= max)) return text(413, "missing or too large");
      const name = url.searchParams.get("name") || "";
      const ext = name.includes(".") ? name.split(".").pop().toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 8) : "";
      const key = newKey(ext);
      await env.FILES.put(key, request.body, { httpMetadata: { contentType: TYPES[ext] || "application/octet-stream" } });
      return Response.json({ key });
    }

    if ((request.method === "GET" || request.method === "HEAD") && url.pathname.startsWith("/f/")) {
      const key = url.pathname.slice(3);
      const object = KEY.test(key) ? await env.FILES.get(key) : null;
      if (!object) return text(404, "not found");
      const type = object.httpMetadata?.contentType || "application/octet-stream";
      const headers = {
        "content-type": type,
        "x-content-type-options": "nosniff",
        "cache-control": "public, max-age=31536000, immutable",
      };
      if (!INLINE.has(type)) headers["content-disposition"] = "attachment";
      return new Response(request.method === "HEAD" ? null : object.body, { headers });
    }

    return text(404, "not found");
  },
};
