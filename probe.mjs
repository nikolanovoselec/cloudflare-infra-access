import { createHash, timingSafeEqual } from 'node:crypto';
import { isIPv4 } from 'node:net';
import { readGreeting } from './greeting.mjs';

const PORT = 8080;
const headers = { 'Cache-Control': 'no-store' };
const resultStages = new Set(['greeting', 'non-ssh', 'tcp-timeout', 'tcp-error', 'tcp-closed']);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function handleRequest(request, env) {
  const { pathname, search } = new URL(request.url);
  const expiry = Number(env.EXPIRES_AT);
  const bearer = /^Bearer ([a-f0-9]{64})$/.exec(request.headers.get('Authorization') ?? '');
  if (request.method !== 'POST' || pathname !== '/probe' || search || request.body !== null ||
      !Number.isSafeInteger(expiry) || Date.now() >= expiry || !bearer ||
      !/^[a-f0-9]{64}$/.test(env.PROBE_TOKEN_HASH ?? '') ||
      !timingSafeEqual(createHash('sha256').update(bearer[1]).digest(), Buffer.from(env.PROBE_TOKEN_HASH, 'hex'))) {
    return new Response('Not found', { status: 404, headers });
  }
  try {
    const result = await env.PROBE.getByName('singleton').probe();
    const status = result.stage === 'already-used' ? 409 : resultStages.has(result.stage) && result.containerStopped ? 200 : 503;
    return Response.json(result, { status, headers });
  } catch {
    return Response.json({ stage: 'durable-object' }, { status: 503, headers });
  }
}

export async function claimProbe(storage) {
  storage.sql.exec('CREATE TABLE IF NOT EXISTS one_shot (id INTEGER PRIMARY KEY)');
  const claimed = storage.sql.exec('INSERT OR IGNORE INTO one_shot VALUES (1) RETURNING id').toArray().length === 1;
  if (claimed) await storage.sync(); // Do not start external I/O until the marker is durable.
  return claimed;
}

function bounded(work, deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) throw Error('Deadline');
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(Error('Deadline')); }, remaining);
  });
  return Promise.race([timeout, Promise.resolve().then(() => work(controller.signal))]).finally(() => {
    clearTimeout(timer);
    controller.abort();
  });
}

async function readResult(container, signal) {
  const port = container.getTcpPort(PORT);
  while (!signal.aborted) {
    let response;
    try {
      response = await port.fetch('http://container/result', { signal, redirect: 'error' });
    } catch {
      // Starting instances may refuse the internal readiness request.
    }
    if (response?.status === 200) {
      const { stage, tcpConnected, sshGreeting } = await response.json();
      if (!resultStages.has(stage) || typeof tcpConnected !== 'boolean' ||
          sshGreeting !== (stage === 'greeting') || (sshGreeting && !tcpConnected)) throw Error('Invalid result');
      return { stage, tcpConnected, sshGreeting };
    }
    await response?.body?.cancel();
    if (!signal.aborted) await delay(250);
  }
  throw Error('Deadline');
}

export async function runProbe({ storage, container }, binding, expiry, {ip, identity} = {}) {
  const deadline = Math.min(expiry, Date.now() + 40_000);
  if (!Number.isSafeInteger(expiry) || Date.now() >= deadline) return { stage: 'expired', registered: false };
  if (typeof identity !== 'string' || !/^[^\s@]+@[^\s@]+$/.test(identity) || identity.length > 254
      || typeof ip !== 'string' || !isIPv4(ip) || !/^(?:10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|192\.168\.)/.test(ip)) {
    return { stage: 'runtime-config', registered: false };
  }
  if (!await claimProbe(storage)) return { stage: 'already-used', registered: false };
  let result = { stage: 'identity-fetcher', registered: false };
  try {
    const fetcher = await bounded(() => binding.newFetcher(identity), deadline);
    result.stage = 'tcp-registration';
    await bounded(() => container.interceptOutboundTcp(ip, fetcher), deadline);
    result = { stage: 'container-start', registered: true, containerStopped: false };
  } catch {
    return result;
  }
  let started = false;
  try {
    if (Date.now() >= deadline || container.running) throw Error('Unavailable or already running');
    container.start({
      image: 'cloudflare/debian-trixie', instance: 'lite', enableInternet: false,
      entrypoint: ['node', '-e', `(${readGreeting.toString()})(${JSON.stringify(ip)}, ${deadline})`],
    });
    started = true;
    const exited = container.monitor().then(
      () => ({ stage: 'container-exit' }), () => ({ stage: 'container-exit' }),
    );
    await bounded(() => container.setInactivityTimeout(30_000), deadline);
    result.stage = 'container-result';
    Object.assign(result, await bounded(signal => Promise.race([readResult(container, signal), exited]), deadline));
  } catch {
    // Return only the current bounded stage, never a platform exception.
  } finally {
    try {
      if (started || container.running) await bounded(() => container.destroy(), Date.now() + 5_000);
      result.containerStopped = !container.running;
    } catch {
      // Keep the shutdown status unconfirmed.
    }
  }
  return result;
}
