import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { claimProbe, handleRequest, runProbe } from './probe.mjs';

const config = { ip: '192.168.250.10', identity: 'tester@example.invalid' };
const token = 'a'.repeat(64);
const auth = { Authorization: `Bearer ${token}` };
const request = (headers, opts, suffix = '') => new Request(`https://test.invalid/probe${suffix}`, {
  method: 'POST', headers, ...opts,
});
const environment = () => ({
  PROBE_TOKEN_HASH: createHash('sha256').update(token).digest('hex'),
  EXPIRES_AT: String(Date.now() + 60_000),
});

function fixture({ registration, mintError, failedStart, result, runtimeConfig = config } = {}) {
  const db = new DatabaseSync(':memory:');
  const storage = {
    sql: { exec(query) {
      if (!query.includes('RETURNING')) { db.exec(query); return; }
      return { toArray: () => db.prepare(query).all() };
    } },
    async sync() {},
  };
  let intercepted = false;
  let state = 'idle';
  const fetcher = {};
  const container = {
    async interceptOutboundTcp(target, boundFetcher) {
      if (target !== config.ip || boundFetcher !== fetcher || state !== 'idle') {
        throw Error('Invalid route');
      }
      await registration;
      intercepted = true;
    },
    get running() { return state === 'running'; },
    start({ enableInternet, image, entrypoint }) {
      if (!intercepted || enableInternet !== false || image !== 'cloudflare/debian-trixie' || entrypoint[0] !== 'node') {
        throw Error('Unsafe start');
      }
      if (failedStart) throw Error('private platform data');
      state = 'running';
    },
    async setInactivityTimeout() {},
    async destroy() { state = 'stopped'; },
    monitor() { return new Promise(() => {}); },
    getTcpPort(port) {
      return { async fetch(url) {
        if (port !== 8080 || url !== 'http://container/result' || state !== 'running') throw Error('Not ready');
        return Response.json(result ?? { stage: 'greeting', tcpConnected: true, sshGreeting: true });
      } };
    },
  };
  const binding = { async newFetcher(identity) {
    if (mintError || identity !== config.identity) throw Error('private platform data');
    return fetcher;
  } };
  return {
    probe: () => runProbe({ storage, container }, binding, Date.now() + 60_000, runtimeConfig),
    state: () => state,
    close: () => db.close(),
    storage,
  };
}

test('invalid, expired, wrong route/method, and nonempty requests get identical private 404s', async () => {
  const env = { ...environment(), PROBE: { getByName() { throw Error('DO was reached'); } } };
  for (const [req, config] of [
    [request(), env],
    [request({ Authorization: `Bearer ${'b'.repeat(64)}` }), env],
    [request(auth), { ...env, EXPIRES_AT: String(Date.now() - 1) }],
    [request(auth), { ...env, PROBE_TOKEN_HASH: '' }],
    [request(auth, {}, '?target=elsewhere'), env],
    [request(auth, { method: 'GET' }), env],
    [request(auth, { body: 'identity=someone-else' }), env],
  ]) {
    const response = await handleRequest(req, config);
    assert.equal(response.status, 404);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(await response.text(), 'Not found');
  }
});

test('concurrent and later authenticated calls cannot launch another container; no request inputs arrive at the DO', async (t) => {
  let release;
  const f = fixture({ registration: new Promise(resolve => { release = resolve; }) });
  t.after(f.close);
  const env = { ...environment(), PROBE: { getByName(name) {
    assert.equal(name, 'singleton');
    return { probe(...args) { assert.deepEqual(args, []); return f.probe(); } };
  } } };
  const respond = headers => handleRequest(request(headers), env);
  const first = respond({ ...auth, 'X-User-Email': 'untrusted', 'X-Destination': 'untrusted' });
  await new Promise(resolve => setImmediate(resolve));
  const concurrent = await respond(auth);
  assert.equal(concurrent.status, 409);
  assert.equal((await concurrent.json()).stage, 'already-used');
  assert.equal(f.state(), 'idle', 'registration must resolve before container startup');
  release();
  const success = await first;
  assert.equal(success.status, 200);
  assert.deepEqual(await success.json(), {
    stage: 'greeting', registered: true, tcpConnected: true, sshGreeting: true, containerStopped: true,
  });
  assert.equal(f.state(), 'stopped');
  assert.equal((await respond(auth)).status, 409);
  assert.equal(await claimProbe(f.storage), false); // a recreated DO sees the same SQLite marker
});

test('fetcher or intercept failure does not start the container and burns the claim', async (t) => {
  for (const [options, stage] of [
    [{ mintError: true }, 'identity-fetcher'],
    [{ get registration() { return { then(_, reject) { reject(Error('private platform data')); } }; } }, 'tcp-registration'],
  ]) {
    const f = fixture(options);
    t.after(f.close);
    assert.deepEqual(await f.probe(), { stage, registered: false });
    assert.equal(f.state(), 'idle');
    assert.deepEqual(await f.probe(), { stage: 'already-used', registered: false });
  }
});

test('registration success is reported even when start fails; listening does not prove SSH greeting', async (t) => {
  const failed = fixture({ failedStart: true });
  t.after(failed.close);
  assert.deepEqual(await failed.probe(), { stage: 'container-start', registered: true, containerStopped: true });
  const connected = fixture({ result: { stage: 'non-ssh', tcpConnected: true, sshGreeting: false, banner: 'private' } });
  t.after(connected.close);
  assert.deepEqual(await connected.probe(), {
    stage: 'non-ssh', registered: true, tcpConnected: true, sshGreeting: false, containerStopped: true,
  });
  assert.equal(connected.state(), 'stopped');
});

test('missing probe identity fails before consuming the one-shot claim', async (t) => {
  const f = fixture({ runtimeConfig: { ip: config.ip } });
  t.after(f.close);
  assert.deepEqual(await f.probe(), { stage: 'runtime-config', registered: false });
  assert.equal(f.state(), 'idle');
  assert.equal(await claimProbe(f.storage), true);
});
