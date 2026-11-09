// Fake OpenClaw gateway for BarClaw development.
// Validates every request against the official protocol schema, checks the device
// signature, and streams a canned reply (with an image artifact) for chat.send.
// Run: npm install && PORT=18799 npm start, then point BarClaw at ws://127.0.0.1:18799
//
// Visual intelligence:
//   MOCK_OUT=<dir>        save screenshots from chat.send attachments and node.invoke results there
//   MOCK_NODE_PAIRING=1   make the node wait for device + command-surface approval (approve via
//                         device.pair.approve / node.pair.approve from an admin operator, or by creating
//                         $MOCK_OUT/approve-device and $MOCK_OUT/approve-surface)
//   A chat.send containing "agent-look" makes the mock ask the node for screen.snapshot, like an agent tool call.

import { WebSocketServer } from 'ws';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';
import crypto from 'node:crypto';
import fs from 'node:fs';
const schema = JSON.parse(fs.readFileSync(new URL('./node_modules/@openclaw/gateway-protocol/protocol.schema.json', import.meta.url)));
const ajv = new Ajv({ strict: false, allErrors: true }); addFormats(ajv);
ajv.addSchema({ $id: 'p', definitions: schema.definitions });
const v = (name, data) => { const f = ajv.getSchema(`p#/definitions/${name}`); if (!f) return `no schema ${name}`; return f(data) ? null : JSON.stringify(f.errors).slice(0, 600); };
const methodSchema = { connect: 'ConnectParams', 'chat.send': 'ChatSendParams', 'chat.history': 'ChatHistoryParams', 'chat.abort': 'ChatAbortParams',
  'sessions.patch': 'SessionsPatchParams', 'sessions.compact': 'SessionsCompactParams', 'sessions.reset': 'SessionsResetParams',
  'sessions.describe': 'SessionsDescribeParams', 'sessions.messages.subscribe': 'SessionsMessagesSubscribeParams', 'models.list': 'ModelsListParams',
  'exec.approval.resolve': 'ExecApprovalResolveParams', 'artifacts.download': 'ArtifactsDownloadParams', 'sessions.list': 'SessionsListParams', 'sessions.create': 'SessionsCreateParams', 'sessions.messages.unsubscribe': 'SessionsMessagesUnsubscribeParams' };
Object.assign(methodSchema, { 'node.pluginTools.update': 'NodePluginToolsUpdateParams', 'node.skills.update': 'NodeSkillsUpdateParams',
  'node.invoke.result': 'NodeInvokeResultParams', 'node.list': 'NodeListParams', 'node.pair.list': 'NodePairListParams',
  'node.pair.approve': 'NodePairApproveParams', 'device.pair.list': 'DevicePairListParams', 'device.pair.approve': 'DevicePairApproveParams' });
const port = Number(process.env.PORT || 18799);
const OUT = process.env.MOCK_OUT;
if (OUT) fs.mkdirSync(OUT, { recursive: true });
const node = { ws: null, deviceId: null, deviceApproved: !process.env.MOCK_NODE_PAIRING, surfaceApproved: !process.env.MOCK_NODE_PAIRING,
  tools: [], skills: [], invokes: 0, connects: 0 };
const fileApproved = (name) => OUT && fs.existsSync(`${OUT}/${name}`);
const saveImage = (name, b64, mime) => { if (!OUT) return; const ext = mime === 'image/png' ? 'png' : 'jpg'; fs.writeFileSync(`${OUT}/${name}.${ext}`, Buffer.from(b64, 'base64')); console.log(`  saved ${OUT}/${name}.${ext}`); };
const invokeNode = (params) => {
  if (!node.ws) return console.log('  no node connected; cannot invoke');
  const id = 'inv-' + (++node.invokes);
  const payload = { id, nodeId: node.deviceId, command: 'screen.snapshot', paramsJSON: JSON.stringify(params), timeoutMs: 30000, idempotencyKey: id };
  const err = v('NodeInvokeRequestEvent', payload);
  console.log(`INVOKE ${id} -> node (${err ? 'SCHEMA-FAIL ' + err : 'schema ok'})`);
  node.ws.send(JSON.stringify({ type: 'event', event: 'node.invoke.request', payload }));
};
const RICH_REPLY = [
  '## Weekly signups', '',
  'Signups grew **100%** in three weeks, with an inline formula $E = mc^2$ and `code` in the text.', '',
  '```chart',
  '{"type":"bar","title":"Weekly signups","x":"Week","y":"Signups","data":[{"x":"W1","y":120},{"x":"W2","y":180},{"x":"W3","y":240}]}',
  '```', '',
  '| Plan | Users | Change |', '| :-- | --: | :-: |', '| Free | 1,204 | +12% |', '| Pro | 318 | +41% |', '',
  '$$\\int_0^1 \\frac{x^2}{\\sqrt{1+x}}\\,dx = \\sum_{n=0}^{\\infty} \\binom{2n}{n} \\frac{a_n}{n!}$$', '',
  '```python', 'def growth(values):', '    # week-over-week change', '    return [b / a - 1 for a, b in zip(values, values[1:])]', '```', '',
  '- [x] Ship the release', '- [ ] Write the *announcement*', '', '> Keep it skimmable.',
].join('\n');
const sessions = [{ key: 'agent:main:main', kind: 'direct', updatedAt: Date.now() - 60000 }];
const wss = new WebSocketServer({ port });
console.log('mock on', port);
wss.on('connection', (ws) => {
  let seq = 0; const nonce = crypto.randomUUID(); const ts = Date.now();
  const event = (e, payload) => ws.send(JSON.stringify({ type: 'event', event: e, payload, seq: ++seq }));
  const res = (id, ok, payload, error) => ws.send(JSON.stringify({ type: 'res', id, ok, payload, error }));
  event('connect.challenge', { nonce, ts });
  ws.on('message', (raw) => {
    const f = JSON.parse(raw.toString());
    const sname = methodSchema[f.method];
    const err = sname ? v(sname, f.params) : null;
    console.log(`REQ ${f.method} ${err ? 'SCHEMA-FAIL ' + err : 'ok'}`);
    if (f.method === 'connect') {
      const d = f.params.device; const p = f.params;
      const payload = ['v2', d.id, p.client.id, p.client.mode, p.role, (p.scopes||[]).join(','), String(d.signedAt), p.auth?.token ?? '', d.nonce].join('|');
      const pub = Buffer.from(d.publicKey.replace(/-/g,'+').replace(/_/g,'/'), 'base64');
      const key = crypto.createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100','hex'), pub]), format: 'der', type: 'spki' });
      const sigOk = crypto.verify(null, Buffer.from(payload), key, Buffer.from(d.signature.replace(/-/g,'+').replace(/_/g,'/'), 'base64'));
      const idOk = crypto.createHash('sha256').update(pub).digest('hex') === d.id;
      console.log(`  signature ${sigOk} deviceId ${idOk} nonce ${d.nonce === nonce} signedAt ${d.signedAt === ts}`);
      // Simulate OpenClaw's scope upgrade: admin needs approval (create $MOCK_ADMIN_APPROVED_FILE to approve).
      const wantsAdmin = (p.scopes || []).includes('operator.admin');
      const approved = process.env.MOCK_ADMIN_APPROVED_FILE ? fs.existsSync(process.env.MOCK_ADMIN_APPROVED_FILE) : true;
      if (wantsAdmin && !approved) {
        console.log('  admin upgrade pending');
        return res(f.id, false, undefined, { code: 'NOT_PAIRED', message: 'pairing required', details: { code: 'PAIRING_REQUIRED', requestId: 'req-admin-1', recommendedNextStep: 'wait_then_retry' } });
      }
      ws.scopes = p.scopes || [];
      ws.role = p.role;
      if (p.role === 'node') {
        node.connects++;
        console.log(`  NODE connect #${node.connects} caps=${JSON.stringify(p.caps)} commands=${JSON.stringify(p.commands)} permissions=${JSON.stringify(p.permissions)} name=${p.client.displayName} mode=${p.client.mode} sameDevice=${d.id === global.operatorDeviceId}`);
        node.deviceId = d.id;
        if (fileApproved('approve-device')) node.deviceApproved = true;
        if (!node.deviceApproved) {
          console.log('  node device approval pending');
          return res(f.id, false, undefined, { code: 'NOT_PAIRED', message: 'pairing required', details: { code: 'PAIRING_REQUIRED', requestId: 'req-node-dev', recommendedNextStep: 'wait_then_retry' } });
        }
        node.ws = ws;
        ws.on('close', () => { if (node.ws === ws) { node.ws = null; node.tools = []; node.skills = []; console.log('  node disconnected'); } });
        return res(f.id, true, { type: 'hello-ok', protocol: 4, server: { version: 'mock-2026.8', connId: 'n1' }, features: { methods: [], events: [] }, snapshot: {}, auth: { role: 'node', scopes: [], deviceToken: 'nodetok' }, policy: { maxPayload: 26214400, maxBufferedBytes: 52428800, tickIntervalMs: 15000 } });
      }
      global.operatorDeviceId = d.id;
      return res(f.id, true, { type: 'hello-ok', protocol: 4, server: { version: 'mock-2026.8', connId: 'c1' }, features: { methods: [], events: [] }, snapshot: {}, auth: { role: 'operator', scopes: p.scopes, deviceToken: 'devtok' }, policy: { maxPayload: 26214400, maxBufferedBytes: 52428800, tickIntervalMs: 15000, attachments: { maxBytes: 20971520, maxImageBytes: 6291456 } } });
    }
    const isAdmin = (ws.scopes || []).includes('operator.admin');
    const forbidden = (scope) => res(f.id, false, undefined, { code: 'FORBIDDEN', message: `missing scope: ${scope}`, details: { code: 'MISSING_SCOPE', missingScope: scope } });
    switch (f.method) {
      case 'node.pluginTools.update':
        if (ws.role !== 'node') return forbidden('node');
        node.tools = f.params.tools;
        console.log(`  tools: ${node.tools.map(t => `${t.name}->${t.command} (plugin ${t.pluginId})`).join(', ')}`);
        return res(f.id, true, { nodeId: node.deviceId, tools: node.surfaceApproved ? node.tools : [] });
      case 'node.skills.update': {
        if (ws.role !== 'node') return forbidden('node');
        node.skills = f.params.skills;
        const sk = node.skills[0];
        console.log(`  skills: ${node.skills.map(s => s.name).join(', ')} contentChars=${sk?.content.length} frontmatter=${sk?.content.startsWith('---\nname: ' + sk?.name)}`);
        return res(f.id, true, { nodeId: node.deviceId, skills: node.skills });
      }
      case 'node.invoke.result': {
        if (ws.role !== 'node') return forbidden('node');
        res(f.id, true, { ok: true });
        const p = f.params;
        if (!p.ok) { console.log(`  INVOKE RESULT error ${JSON.stringify(p.error)}`); return; }
        const pl = p.payload || {};
        const snapshotOk = (pl.format === 'jpeg' || pl.format === 'png') && typeof pl.base64 === 'string' && pl.base64.length > 0;
        const content = Array.isArray(pl.content) ? pl.content : [];
        const img = content.find(c => c.type === 'image');
        const txt = content.find(c => c.type === 'text');
        console.log(`  INVOKE RESULT screenSnapshotContract=${snapshotOk} agentToolResult=${Array.isArray(pl.content)} image=${!!img} sameImage=${img?.data === pl.base64} ${pl.width}x${pl.height} bytes=${Buffer.from(pl.base64 || '', 'base64').length}`);
        console.log('  TOOL TEXT: ' + (txt?.text || '').slice(0, 600).replace(/\n/g, ' | '));
        if (img) saveImage(`invoke-${p.id}`, img.data, img.mimeType);
        return;
      }
      case 'node.list': {
        if (fileApproved('approve-surface')) node.surfaceApproved = true;
        const nodes = node.deviceId ? [{ nodeId: node.deviceId, displayName: 'BarClaw', platform: 'linux', connected: !!node.ws, paired: node.deviceApproved,
          commands: node.ws && node.surfaceApproved ? ['screen.snapshot'] : [],
          ...(node.ws && !node.surfaceApproved && node.deviceId === global.operatorDeviceId ? { pendingRequestId: 'req-node-surface' } : {}) }] : [];
        return res(f.id, true, { ts: Date.now(), nodes });
      }
      case 'device.pair.list':
        if (!isAdmin) return forbidden('operator.pairing');
        return res(f.id, true, { pending: node.deviceApproved || !node.deviceId ? [] : [{ requestId: 'req-node-dev', deviceId: node.deviceId, role: 'node', roles: ['operator', 'node'], ts: Date.now() }], paired: [] });
      case 'device.pair.approve':
        if (!isAdmin) return forbidden('operator.pairing');
        if (f.params.requestId === 'req-node-dev') { node.deviceApproved = true; console.log('  APPROVED node device'); }
        return res(f.id, true, { requestId: f.params.requestId, device: { deviceId: node.deviceId } });
      case 'node.pair.list':
        if (!isAdmin) return forbidden('operator.pairing');
        return res(f.id, true, { pending: node.surfaceApproved || !node.ws ? [] : [{ requestId: 'req-node-surface', nodeId: node.deviceId, commands: ['screen.snapshot'], ts: Date.now() }], paired: [] });
      case 'node.pair.approve':
        if (!isAdmin) return forbidden('operator.pairing');
        if (f.params.requestId === 'req-node-surface') { node.surfaceApproved = true; console.log('  APPROVED node surface'); }
        return res(f.id, true, { requestId: f.params.requestId, node: { nodeId: node.deviceId } });
      case 'chat.history':
        if (process.env.MOCK_LONG_HISTORY) {
          // 80 tool-heavy turns: user, tool call, 2 tool results, answer (2 of 5 rows visible).
          const rows = [];
          for (let t = 1; t <= 80; t++) {
            rows.push({ role: 'user', content: [{ type: 'text', text: `Question ${t}` }], __openclaw: { id: `u${t}` } });
            rows.push({ role: 'assistant', content: [{ type: 'toolCall', name: 'exec', arguments: {} }], __openclaw: { id: `c${t}` } });
            rows.push({ role: 'toolResult', content: [{ type: 'text', text: 'ok' }], __openclaw: { id: `r${t}a` } });
            rows.push({ role: 'toolResult', content: [{ type: 'text', text: 'ok' }], __openclaw: { id: `r${t}b` } });
            rows.push({ role: 'assistant', content: [{ type: 'text', text: `Answer ${t}` }], __openclaw: { id: `a${t}` } });
          }
          const limit = f.params.limit || 40, offset = f.params.offset || 0, total = rows.length;
          const end = total - offset, start = Math.max(0, end - limit);
          const hasMore = start > 0;
          return res(f.id, true, { messages: rows.slice(start, end), hasMore, ...(hasMore ? { nextOffset: offset + (end - start) } : {}), totalMessages: total, sessionKey: f.params.sessionKey });
        }
        return res(f.id, true, { messages: [{ role: 'user', content: [{ type: 'text', text: '<barclaw-format>x</barclaw-format>\n\nold question' }] }, { role: 'assistant', content: [{ type: 'text', text: 'old **answer**' }] }], sessionInfo: { model: 'gpt-5.1-codex', modelProvider: 'openai-codex', thinkingLevel: 'medium', totalTokens: 1000, contextTokens: 200000 } });
      case 'sessions.describe': return res(f.id, true, { session: { key: 'agent:main:main', model: 'gpt-5.1-codex', modelProvider: 'openai-codex', thinkingLevel: 'medium' } });
      case 'models.list': return res(f.id, true, { models: [{ id: 'gpt-5.1-codex', name: 'GPT-5.1 Codex', provider: 'openai-codex' }, { id: 'claude-sonnet-5-5', name: 'Claude Sonnet 5.5', provider: 'anthropic' }] });
      case 'usage.status': return res(f.id, true, { updatedAt: Date.now(), providers: [{ provider: 'openai', displayName: 'Codex', plan: 'plus', windows: [{ label: '5h', usedPercent: 37, resetAt: Date.now() + 3600e3 }, { label: 'Week', usedPercent: 64 }] }] });
      case 'chat.send': {
        const runId = 'run-' + seq;
        res(f.id, true, { runId, status: 'started' });
        console.log('  message starts with instruction:', f.params.message.startsWith('<barclaw-format>'));
        const atts = f.params.attachments || [];
        console.log(`  attachments=${atts.length} screenBlock=${f.params.message.startsWith('<barclaw-screen')} userText=${JSON.stringify(f.params.message.split('</barclaw-format>').pop().trim().slice(0, 80))}`);
        atts.forEach((a, i) => { console.log(`  attachment ${i}: type=${a.type} mime=${a.mimeType} name=${a.fileName} ${a.width}x${a.height} bytes=${Buffer.from(a.content || '', 'base64').length}`); saveImage(`attach-${seq}-${i}`, a.content, a.mimeType); });
        if (f.params.message.startsWith('<barclaw-screen')) console.log('  SCREEN BLOCK: ' + f.params.message.split('</barclaw-screen>')[0].slice(0, 700).replace(/\n/g, ' | '));
        if (f.params.message.includes('agent-look')) setTimeout(() => invokeNode({ maxWidth: 1200 }), 300);
        const sk = 'agent:main:main'; let rs = 0;
        // A message containing "rich-demo" gets a reply exercising every renderer.
        if (f.params.message.includes('rich-demo')) {
          const rich = RICH_REPLY;
          setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'status', phase: 'starting_model' }), 100);
          setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'delta', message: { role: 'assistant', content: [{ type: 'text', text: rich.slice(0, 80) }] } }), 400);
          setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'final', message: { role: 'assistant', content: [{ type: 'text', text: rich }] } }), 800);
          return;
        }
        // With MOCK_PROGRESS=1, narrate the run like a real agent before replying.
        const lag = process.env.MOCK_PROGRESS ? 4000 : 0;
        if (lag) {
          const agent = (at, stream, data) => setTimeout(() => event('agent', { runId, sessionKey: sk, seq: at, stream, ts: Date.now(), data }), at);
          agent(150, 'lifecycle', { phase: 'start' });
          agent(400, 'item', { kind: 'preamble', title: 'Preamble', phase: 'update', itemId: 'p1', progressText: "I'll check the forecast and your calendar, then summarize." });
          agent(600, 'plan', { phase: 'update', steps: [{ step: 'Look up the weather', status: 'in_progress' }, { step: 'Check tomorrow\'s calendar', status: 'pending' }, { step: 'Summarize', status: 'pending' }] });
          agent(800, 'tool', { phase: 'start', name: 'web_search', toolCallId: 't1', args: { query: 'seoul weather tomorrow' } });
          agent(850, 'item', { kind: 'tool', itemId: 'i1', toolCallId: 't1', phase: 'start', title: 'Searching the web for “seoul weather tomorrow”', name: 'web_search' });
          agent(1800, 'tool', { phase: 'result', name: 'web_search', toolCallId: 't1' });
          agent(1850, 'item', { kind: 'tool', itemId: 'i1', toolCallId: 't1', phase: 'end', title: 'Searched the web for “seoul weather tomorrow”', status: 'completed' });
          agent(2000, 'plan', { phase: 'update', steps: [{ step: 'Look up the weather', status: 'completed' }, { step: 'Check tomorrow\'s calendar', status: 'in_progress' }, { step: 'Summarize', status: 'pending' }] });
          agent(2100, 'tool', { phase: 'start', name: 'exec', toolCallId: 't2', args: { command: 'gcal list --tomorrow' } });
          agent(3300, 'tool', { phase: 'result', name: 'exec', toolCallId: 't2' });
        }
        setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'status', phase: 'starting_model' }), 100);
        setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'delta', deltaText: '## Hi', message: { role: 'assistant', content: [{ type: 'text', text: '## Hi' }] } }), 600 + lag);
        setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'delta', deltaText: ' there\n\n- one\n- two' }), 900 + lag);
        setTimeout(() => event('chat', { runId, sessionKey: sk, seq: rs++, state: 'final', message: { role: 'assistant', content: [{ type: 'text', text: '## Hi there\n\n- one\n- two' }, { type: 'image', artifactId: 'art_1', mimeType: 'image/png', width: 1, height: 1 }] } }), 1200 + lag);
        if (process.env.MOCK_IMAGE_LATE) {
          // Like a real codex imagegen run: the live final carries only the caption, and the
          // image is committed to the transcript afterwards under an internal run id.
          setTimeout(() => event('session.message', { sessionKey: sk, messageId: 'm-img', message: {
            role: 'assistant', __openclaw: { runId: 'internal-run-1', id: 'm-img' },
            content: [{ type: 'text', text: '- two' }, { type: 'image', artifactId: 'art_late', mimeType: 'image/png', width: 1, height: 1 }] } }), 2500 + lag);
        }
        if (process.env.MOCK_REPLAY) {
          // Things a real gateway can deliver after a reply: a repeated terminal event,
          // the same reply committed under another run id, and a silent run.
          const finalMsg = { role: 'assistant', content: [{ type: 'text', text: '## Hi there\n\n- one\n- two' }] };
          setTimeout(() => event('chat', { runId, sessionKey: sk, seq: 90, state: 'final', message: finalMsg }), 3500);
          setTimeout(() => event('chat', { runId: runId + '-mirror', sessionKey: sk, seq: 0, state: 'final', message: finalMsg }), 4000);
          setTimeout(() => event('chat', { runId: 'heartbeat-1', sessionKey: sk, seq: 0, state: 'final', message: { role: 'assistant', content: [] } }), 4500);
        }
        return;
      }
      case 'artifacts.download': return res(f.id, true, { artifact: { id: f.params.artifactId, type: 'image', title: 'img', mimeType: 'image/png', download: { mode: 'bytes' } }, encoding: 'base64', data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==' });
      case 'sessions.list': return res(f.id, true, { sessions: [...sessions].sort((a, b) => b.updatedAt - a.updatedAt), count: sessions.length });
      case 'sessions.create': {
        const key = 'agent:main:s-' + Math.random().toString(36).slice(2, 8);
        sessions.push({ key, kind: 'direct', label: 'Fresh chat', updatedAt: Date.now() });
        return res(f.id, true, { ok: true, key, sessionId: key, entry: { key } });
      }
      case 'sessions.reset':
      case 'sessions.compact':
        if (!(ws.scopes || []).includes('operator.admin')) {
          return res(f.id, false, undefined, { code: 'FORBIDDEN', message: 'missing scope: operator.admin', details: { code: 'MISSING_SCOPE', missingScope: 'operator.admin', requiredScopes: ['operator.admin'] } });
        }
        return res(f.id, true, { ok: true, key: f.params.key });
      default: return res(f.id, true, { ok: true });
    }
  });
});
