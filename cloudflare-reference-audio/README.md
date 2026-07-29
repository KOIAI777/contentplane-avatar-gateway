# ContentPlane R2 Reference Audio Gateway

Cloudflare Worker for temporary reference-audio uploads required by speech providers such as Gitee IndexTTS-2.

The Worker is intentionally independent from the GPU avatar gateway. It stores audio in a private R2 bucket and exposes the same contract used by ContentPlane:

```text
POST   /v1/reference-audio
GET    /v1/reference-audio/{id}?expires=...&signature=...
DELETE /v1/reference-audio/{id}
```

Uploaded objects expire after 15 minutes. The Worker also runs a 15-minute cleanup cron for files left behind after a failed client request.

## Setup

From this directory:

```bash
npm install
npx wrangler types
npx wrangler secret put GATEWAY_API_TOKEN
npx wrangler secret put DOWNLOAD_SIGNING_SECRET
npm run check
npm run deploy:dry
npm run deploy
```

Use different random values for the two secrets. `GATEWAY_API_TOKEN` is the token entered in ContentPlane's “文件中转” Provider. `DOWNLOAD_SIGNING_SECRET` never leaves the Worker.

The R2 bucket is configured in `wrangler.jsonc` as `contentplane-reference-audio`. Keep it private; do not add a public R2 URL or public bucket route.

## Local test

```bash
npm install
npx wrangler dev
curl http://127.0.0.1:8787/health
```

For remote R2 testing, use a `.dev.vars` file that is ignored by Git and run `wrangler dev --remote`.
