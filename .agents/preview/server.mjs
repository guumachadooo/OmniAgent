import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT_DIR = path.resolve(__dirname, '../..');
const PORT = Number(process.env.PORT || 3000);
const HOST = '0.0.0.0';

const RUNTIME_ENV_BRIDGE_PATH = '/tmp/.cavern_runtime_env.json';
const FEDERATION_STATUS_PATH = '/tmp/cavern-federation/federation_status.json';
const FEDERATION_SCRIPT_PATH = path.join(ROOT_DIR, 'agent', 'repo_federation.py');
const WHITELISTED_RUNTIME_KEYS = [
  'GITHUB_TOKEN',
  'GH_TOKEN',
  'CLOUDFLARE_API_TOKEN',
  'CLOUDFLARE_ACCOUNT_ID',
  'CLOUDFLARE_WORKER_NAME',
  'CAVERN_BRIDGE_PAIR_SECRET',
  'CAVERN_RELAY_URL',
  'CAVERN_ADMIN_KEY',
  'CAVERN_KNOCK_ID',
];

function syncPrivateRuntimeEnvBridge() {
  try {
    const payload = {};
    for (const key of WHITELISTED_RUNTIME_KEYS) {
      const val = String(process.env[key] || '').trim();
      if (val) payload[key] = val;
    }
    if (Object.keys(payload).length > 0) {
      fs.writeFileSync(RUNTIME_ENV_BRIDGE_PATH, JSON.stringify(payload), { mode: 0o600 });
      try { fs.chmodSync(RUNTIME_ENV_BRIDGE_PATH, 0o600); } catch {}
    }
  } catch {}
}

function warmFederatedRepos() {
  if (process.env.CAVERN_DISABLE_FEDERATION_WARM === '1') return;
  if (!fs.existsSync(FEDERATION_SCRIPT_PATH)) return;
  try {
    const child = spawn('python3', ['-B', FEDERATION_SCRIPT_PATH, '--warm', '--workspace', ROOT_DIR], {
      cwd: ROOT_DIR,
      stdio: 'ignore',
      env: { ...process.env },
    });
    child.unref();
  } catch {}
}

function readFederationStatus() {
  try {
    if (fs.existsSync(FEDERATION_STATUS_PATH)) {
      return JSON.parse(fs.readFileSync(FEDERATION_STATUS_PATH, 'utf8'));
    }
  } catch {}
  return { ok: true, status: 'warming', mountRoot: '/tmp/cavern-federation' };
}

syncPrivateRuntimeEnvBridge();
warmFederatedRepos();

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
  if (url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify({ ok: true, service: 'omniagent-workspace-preview', port: PORT }));
    return;
  }
  if (url.pathname === '/api/federation') {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(readFederationStatus()));
    return;
  }
  if (url.pathname === '/favicon.ico') {
    res.writeHead(204);
    res.end();
    return;
  }
  const fed = readFederationStatus();
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(`<!doctype html><html lang="en"><head><meta charset="utf-8"><title>OmniAgent Workspace</title></head><body style="font-family:system-ui,sans-serif;background:#0b0f17;color:#e6edf3;padding:24px"><h1>OmniAgent — Federated Workspace Preview</h1><p>Real stack: C#/.NET Android (<code>OmniAgentAndroid/</code>), Cloudflare Hub v6 (<code>worker/</code>), &amp; Python 3 daemon (<code>agent/</code>).</p><pre>${JSON.stringify(fed, null, 2)}</pre></body></html>`);
});

server.listen(PORT, HOST, () => {
  console.log(`OmniAgent workspace preview listening at http://${HOST}:${PORT}`);
});
