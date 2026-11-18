/**
 * AgentBus Official Hook for Huawei CodeArts Agent
 * Protocol: agentbus-trigger/1
 * Source: Derived from verified 20261024 CodeArts directed recheck fixture
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export const norm = s => typeof s === 'string' ? s.replace(/\\/g, '/').replace(/\/$/, '').toLowerCase() : '';

export async function createHooks(ctx, { root }) {
  const control = path.join(root, '.agentbus');
  const bridgeDir = path.join(control, 'triggers', 'bridge', 'codearts');
  const requestsDir = path.join(bridgeDir, 'requests');
  const claimsDir = path.join(bridgeDir, 'claims');
  for (const dir of [requestsDir, claimsDir, path.join(bridgeDir, 'outcomes'), path.join(bridgeDir, 'evidence')]) {
    await fs.mkdir(dir, { recursive: true });
  }
  const instanceId = 'inst_codearts_' + randomUUID().slice(0, 8);
  let ticking = false;
  let active = null;
  let lastActiveSessionId = null;

  async function put(file, data) {
    const target = path.join(bridgeDir, file);
    const temporary = target + '.' + randomUUID() + '.tmp';
    try {
      await fs.writeFile(temporary, JSON.stringify({ at: new Date().toISOString(),
        instance_id: instanceId, ...data }, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' });
      await fs.rename(temporary, target);
    } finally {
      await fs.unlink(temporary).catch(err => { if (err.code !== 'ENOENT') throw err; });
    }
  }
  async function stopped() {
    try { await fs.stat(path.join(control, 'STOP')); return true; }
    catch (err) { if (err.code !== 'ENOENT') return true; }
    try {
      const policy = JSON.parse(await fs.readFile(path.join(control, 'triggers', 'policy.json'), 'utf8'));
      return !policy || typeof policy !== 'object' || Array.isArray(policy) ||
        (policy.stopped !== undefined && policy.stopped !== false) ||
        (policy.delivery_enabled !== undefined && policy.delivery_enabled !== true);
    } catch (err) { return err.code !== 'ENOENT'; }
  }
  async function publishHealth() {
    await put('evidence/READY.json', { workspace: root,
      client_get: typeof ctx.client?.session?.get === 'function',
      client_prompt: typeof ctx.client?.session?.prompt === 'function', busy: !!active,
      listener_alive: true, last_active_session_id: lastActiveSessionId });
  }
  async function tick() {
    if (ticking) return;
    ticking = true;
    let dispatchLock = null;
    try {
      await publishHealth();
      if (await stopped()) return;
      try {
        dispatchLock = await fs.open(path.join(bridgeDir, 'dispatch.lock'), 'wx');
        await dispatchLock.writeFile(JSON.stringify({ instance_id: instanceId,
          claimed_at: new Date().toISOString() }));
        await dispatchLock.sync();
      } catch (err) {
        if (err.code === 'EEXIST') return;
        throw err;
      }
      const files = (await fs.readdir(requestsDir)).sort();
      for (const file of files) {
        if (!/^[A-Za-z0-9_-]+\.json$/.test(file)) continue;
        const reqId = file.slice(0, -5);
        // Permanent exclusive claim is the cross-instance at-most-once fence.
        // A crashed owner leaves uncertainty; nobody guesses that retry is safe.
        let claim;
        try { claim = await fs.open(path.join(claimsDir, reqId + '.claim'), 'wx'); }
        catch (err) { if (err.code === 'EEXIST') continue; throw err; }
        await claim.writeFile(JSON.stringify({ request_id: reqId, instance_id: instanceId,
          claimed_at: new Date().toISOString() }));
        await claim.sync();
        await claim.close();
        let req = null;
        try {
          const reqPath = path.join(requestsDir, file);
          if ((await fs.stat(reqPath)).size > 262144) throw new Error('RequestTooLarge');
          req = JSON.parse(await fs.readFile(reqPath, 'utf8'));
          if (req.request_id !== reqId || typeof req.native_conversation_id !== 'string' ||
              !req.native_conversation_id || typeof req.prompt_summary !== 'string') throw new Error('InvalidRequest');
          const reject = async reason => put(`outcomes/${reqId}.json`, {
            request_id: reqId, session_id: req.native_conversation_id, status: 'rejected', reason });
          if (await stopped()) { await reject('STOPPED'); break; }
          if (typeof ctx.client?.session?.get !== 'function' || typeof ctx.client?.session?.prompt !== 'function') {
            await reject('HOST_API_UNAVAILABLE'); continue;
          }
          const pre = await ctx.client.session.get({ path: { id: req.native_conversation_id }, query: { directory: root } });
          if (pre?.error || pre?.data?.id !== req.native_conversation_id) {
            await reject('TARGET_SESSION_MISMATCH'); continue;
          }
          if (await stopped()) { await reject('STOPPED'); break; }
          active = { id: reqId, native_id: req.native_conversation_id };
          let response;
          try {
            response = await ctx.client.session.prompt({ path: { id: req.native_conversation_id },
              query: { directory: root }, body: { parts: [{ type: 'text', text: req.prompt_summary }] } });
          } catch (err) {
            await put(`outcomes/${reqId}.json`, { request_id: reqId, session_id: req.native_conversation_id,
              status: 'uncertain', error_type: err.name || 'HostPromptError' });
            continue;
          } finally { active = null; }
          await put(`outcomes/${reqId}.json`, { request_id: reqId, session_id: req.native_conversation_id,
            status: response?.error ? 'uncertain' : 'sdk_returned',
            assistant_id: response?.data?.info?.id || null });
          // One native turn per scan, even when multiple requests are waiting.
          break;
        } catch (err) {
          await put(`outcomes/${reqId}.json`, { request_id: reqId,
            session_id: req?.native_conversation_id || null, status: 'rejected',
            reason: 'INVALID_REQUEST_OR_PREFLIGHT', error_type: err.name || 'RequestError' });
        }
      }
    } catch (err) {
      await put('evidence/LAST_ERROR.json', { error_type: err.name || 'HookError' });
    } finally {
      if (dispatchLock) {
        await dispatchLock.close();
        await fs.unlink(path.join(bridgeDir, 'dispatch.lock'));
      }
      ticking = false;
    }
  }
  const timer = setInterval(() => { void tick(); }, 500);
  timer.unref?.();
  void tick();
  const finished = async input => {
    await put('evidence/LAST_TURN_END.json', {
      session_id: input?.session_id || input?.sessionId || ctx.session?.id || lastActiveSessionId,
      request_id: active?.id || null, turn_status: 'finished' });
  };
  return {
    'chat.message': async input => {
      lastActiveSessionId = input?.session_id || input?.sessionId || ctx.session?.id || null;
      await publishHealth();
    },
    'chat.finished': finished,
    'turn.end': finished,
    'event': async () => {}
  };
}
