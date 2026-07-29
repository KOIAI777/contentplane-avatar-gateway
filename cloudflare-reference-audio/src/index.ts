const REFERENCE_PREFIX = "reference-audio/";
const REFERENCE_TTL_SECONDS = 15 * 60;
const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
const MAX_MULTIPART_BYTES = MAX_AUDIO_BYTES + 1024 * 1024;

const AUDIO_EXTENSIONS: Record<string, string> = {
  "audio/aac": "aac",
  "audio/flac": "flac",
  "audio/mp4": "m4a",
  "audio/mpeg": "mp3",
  "audio/ogg": "ogg",
  "audio/wav": "wav",
  "audio/wave": "wav",
  "audio/webm": "webm",
  "audio/x-m4a": "m4a",
  "audio/x-wav": "wav",
};

const ALLOWED_AUDIO_TYPES = new Set(Object.keys(AUDIO_EXTENSIONS));

type RuntimeEnv = Env & {
  GATEWAY_API_TOKEN: string;
  DOWNLOAD_SIGNING_SECRET: string;
};

export default {
  async fetch(request: Request, env: RuntimeEnv): Promise<Response> {
    try {
      if (request.method === "OPTIONS") return withCors(new Response(null, { status: 204 }));

      const url = new URL(request.url);
      if (url.pathname === "/health" && request.method === "GET") {
        return json({
          ok: true,
          service: "contentplane-reference-audio",
          configured: Boolean(env.GATEWAY_API_TOKEN && env.DOWNLOAD_SIGNING_SECRET),
        });
      }

      const route = referenceRoute(url.pathname);
      if (!route) return json({ error: "Not found" }, 404);

      if (route.id === undefined && request.method === "POST") {
        requireAuthentication(request, env);
        return await uploadReferenceAudio(request, env, url.origin);
      }

      if (route.id !== undefined && request.method === "GET") {
        return await downloadReferenceAudio(request, env, route.id, url);
      }

      if (route.id !== undefined && request.method === "DELETE") {
        requireAuthentication(request, env);
        await env.REFERENCE_AUDIO.delete(referenceKey(route.id));
        return withCors(new Response(null, { status: 204 }));
      }

      return json({ error: "Method not allowed" }, 405, { Allow: "GET, POST, DELETE, OPTIONS" });
    } catch (error) {
      if (error instanceof HttpError) return json({ error: error.message }, error.status);
      console.error("reference-audio request failed", error);
      return json({ error: "文件中转服务内部错误" }, 500);
    }
  },

  async scheduled(_controller: ScheduledController, env: RuntimeEnv, _ctx: ExecutionContext): Promise<void> {
    let cursor: string | undefined;
    const now = Date.now();

    do {
      const listed = await env.REFERENCE_AUDIO.list({
        prefix: REFERENCE_PREFIX,
        limit: 1000,
        cursor,
        include: ["customMetadata"],
      });
      const expiredKeys = listed.objects
        .filter((object) => Number(object.customMetadata?.expiresAt || 0) <= now)
        .map((object) => object.key);
      if (expiredKeys.length > 0) await env.REFERENCE_AUDIO.delete(expiredKeys);
      cursor = listed.truncated ? listed.cursor : undefined;
    } while (cursor);
  },
};

async function uploadReferenceAudio(request: Request, env: RuntimeEnv, origin: string): Promise<Response> {
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (Number.isFinite(contentLength) && contentLength > MAX_MULTIPART_BYTES) {
    throw new HttpError(413, "参考音频不能超过 20 MB");
  }

  const form = await request.formData();
  const input = form.get("audio") || form.get("file");
  if (!(input instanceof File)) throw new HttpError(400, "请使用 audio 字段上传参考音频");
  if (input.size <= 0) throw new HttpError(400, "参考音频文件为空");
  if (input.size > MAX_AUDIO_BYTES) throw new HttpError(413, "参考音频不能超过 20 MB");

  const contentType = normalizeAudioType(input.type, input.name);
  if (!ALLOWED_AUDIO_TYPES.has(contentType)) {
    throw new HttpError(415, "只支持 MP3、WAV、M4A、AAC、FLAC、OGG 或 WEBM 音频");
  }

  const id = crypto.randomUUID();
  const expiresAt = Date.now() + REFERENCE_TTL_SECONDS * 1000;
  const fileName = safeFileName(input.name || `reference-${id}.${AUDIO_EXTENSIONS[contentType]}`);
  const key = referenceKey(id);
  await env.REFERENCE_AUDIO.put(key, input.stream(), {
    httpMetadata: { contentType },
    customMetadata: {
      expiresAt: String(expiresAt),
      fileName,
      contentType,
    },
  });

  const signature = await signDownload(id, expiresAt, env.DOWNLOAD_SIGNING_SECRET);
  return json({
    id,
    url: `${origin}${referencePath(id)}?expires=${expiresAt}&signature=${encodeURIComponent(signature)}`,
    expires_at: new Date(expiresAt).toISOString(),
  }, 201);
}

async function downloadReferenceAudio(request: Request, env: RuntimeEnv, id: string, url: URL): Promise<Response> {
  const expiresAt = Number(url.searchParams.get("expires") || 0);
  const signature = url.searchParams.get("signature") || "";
  if (!expiresAt || expiresAt <= Date.now() || !signature) {
    await env.REFERENCE_AUDIO.delete(referenceKey(id));
    throw new HttpError(404, "参考音频已过期");
  }
  if (!await verifyDownload(id, expiresAt, signature, env.DOWNLOAD_SIGNING_SECRET)) {
    throw new HttpError(403, "参考音频签名无效");
  }

  const object = await env.REFERENCE_AUDIO.get(referenceKey(id));
  if (!object) throw new HttpError(404, "参考音频不存在");
  if (Number(object.customMetadata?.expiresAt || 0) <= Date.now()) {
    await env.REFERENCE_AUDIO.delete(referenceKey(id));
    throw new HttpError(404, "参考音频已过期");
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Content-Type", object.httpMetadata?.contentType || "application/octet-stream");
  headers.set("Content-Length", String(object.size));
  headers.set("Cache-Control", "private, max-age=60");
  headers.set("Content-Disposition", `inline; filename="${safeHeaderFileName(object.customMetadata?.fileName || `${id}.audio`)}"`);
  headers.set("X-Content-Type-Options", "nosniff");
  return withCors(new Response(object.body, { headers }));
}

function referenceRoute(pathname: string): { id?: string } | null {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length === 2 && parts[0] === "v1" && parts[1] === "reference-audio") return {};
  if (parts.length === 3 && parts[0] === "v1" && parts[1] === "reference-audio" && isReferenceId(parts[2])) {
    return { id: parts[2] };
  }
  return null;
}

function referencePath(id: string): string {
  return `/v1/reference-audio/${encodeURIComponent(id)}`;
}

function referenceKey(id: string): string {
  return `${REFERENCE_PREFIX}${id}`;
}

function isReferenceId(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function normalizeAudioType(type: string, fileName: string): string {
  const normalized = type.toLowerCase().split(";", 1)[0].trim();
  if (ALLOWED_AUDIO_TYPES.has(normalized)) return normalized;
  const extension = fileName.toLowerCase().split(".").pop();
  if (extension === "mp3") return "audio/mpeg";
  if (extension === "wav") return "audio/wav";
  if (extension === "m4a") return "audio/mp4";
  if (extension === "aac") return "audio/aac";
  if (extension === "flac") return "audio/flac";
  if (extension === "ogg") return "audio/ogg";
  if (extension === "webm") return "audio/webm";
  return normalized;
}

function requireAuthentication(request: Request, env: RuntimeEnv): void {
  if (!env.GATEWAY_API_TOKEN || !constantTimeEqual(request.headers.get("authorization") || "", `Bearer ${env.GATEWAY_API_TOKEN}`)) {
    throw new HttpError(401, "文件中转 Token 无效");
  }
}

async function signDownload(id: string, expiresAt: number, secret: string): Promise<string> {
  if (!secret) throw new HttpError(503, "文件中转服务尚未配置签名密钥");
  const key = await hmacKey(secret);
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${id}:${expiresAt}`));
  return base64Url(new Uint8Array(signature));
}

async function verifyDownload(id: string, expiresAt: number, signature: string, secret: string): Promise<boolean> {
  if (!secret) throw new HttpError(503, "文件中转服务尚未配置签名密钥");
  const key = await hmacKey(secret);
  const expected = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${id}:${expiresAt}`));
  return constantTimeEqual(base64Url(new Uint8Array(expected)), signature);
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let result = leftBytes.length ^ rightBytes.length;
  const length = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index += 1) {
    result |= (leftBytes[index] || 0) ^ (rightBytes[index] || 0);
  }
  return result === 0;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function safeFileName(value: string): string {
  const normalized = value.replace(/[\u0000-\u001f\\/]+/g, "_").trim();
  return normalized.slice(0, 120) || "reference-audio";
}

function safeHeaderFileName(value: string): string {
  return safeFileName(value).replace(/["\r\n]/g, "_");
}

function json(value: unknown, status = 200, headers?: HeadersInit): Response {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("Content-Type", "application/json; charset=utf-8");
  return withCors(new Response(JSON.stringify(value), { status, headers: responseHeaders }));
}

function withCors(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type");
  headers.set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  headers.set("Access-Control-Max-Age", "86400");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}
