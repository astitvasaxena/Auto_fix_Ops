// Dashboard tests for the Stage 2 AI Remediation panel. Run: node --test tests/
// No dependencies: node:test + a stubbed global fetch. The backend enforces every
// rule; these tests check that the UI asks it correctly and never offers apply/deploy.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  autoStartKey, canRetry, canShowDiff, canStartManually, diffLines, evidenceView, formatConfidence, isInProgress,
  needsApproval, reasonText, shouldAutoStart,
} from '../lib/remediation.mjs';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

function view({ confidence = 0.9, route, decision, state = 'NOT_REQUESTED', status = 'DIAGNOSED', proposal = null, proposalStatus = null, auto = true } = {}) {
  route = route || (confidence >= 0.8 ? 'AUTOMATIC_AI' : 'HUMAN_APPROVAL_REQUIRED');
  decision = decision || (route === 'AUTOMATIC_AI' ? 'AUTO' : 'PENDING');
  return {
    incident_id: 'inc-1', incident_status: status, auto_generation_enabled: auto,
    routing: { route, decision, report_sha256: 'abc', diagnosis_confidence: confidence, generation_state: state, proposal_status: proposalStatus },
    latest_proposal: proposal,
  };
}

const VALIDATED = { effective_status: 'VALIDATED', unified_diff: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-  memory: "100Mi"\n+  memory: "150Mi"\n' };

test('high confidence starts generation once per diagnosis version', () => {
  const requested = new Set();
  const v = view({ confidence: 0.8 });
  assert.equal(shouldAutoStart(v, requested), true);
  requested.add(autoStartKey(v));
  assert.equal(shouldAutoStart(v, requested), false, 'same page: no second request');
  // After a refresh the set is empty, but the backend reports the claim: still no request.
  for (const state of ['QUEUED', 'RUNNING', 'COMPLETED']) {
    assert.equal(shouldAutoStart(view({ confidence: 0.8001, state }), new Set()), false, state);
  }
  assert.equal(needsApproval(v), false, 'no approval controls on the automatic route');
  // A new diagnosis version is a new key.
  assert.notEqual(autoStartKey(v), autoStartKey({ ...v, routing: { ...v.routing, report_sha256: 'def' } }));
});

test('PATCH_PROPOSALS_AUTO=false: no automatic start, an explicit button instead', () => {
  const off = view({ confidence: 0.95, auto: false });
  assert.equal(shouldAutoStart(off, new Set()), false);
  assert.equal(canStartManually(off), true);
  assert.equal(shouldAutoStart({ ...off, auto_generation_enabled: undefined }, new Set()), false, 'missing flag = off');
  assert.equal(canStartManually(view({ confidence: 0.5, auto: false })), false, 'low confidence needs approval, not a start button');
  assert.equal(canStartManually(view({ confidence: 0.95, state: 'QUEUED' })), false);
});

test('low confidence shows approval controls and never auto-starts', () => {
  const v = view({ confidence: 0.7999 });
  assert.equal(needsApproval(v), true);
  assert.equal(shouldAutoStart(v, new Set()), false);
  assert.equal(needsApproval(view({ confidence: 0.5, decision: 'APPROVED', state: 'QUEUED' })), false);
  assert.equal(needsApproval(view({ confidence: 0.5, decision: 'REJECTED', state: 'REJECTED_BY_HUMAN' })), false);
  assert.equal(shouldAutoStart(view({ confidence: 0.5, decision: 'APPROVED' }), new Set()), false,
    'approved generation is started by the approve call, not by page load');
  assert.equal(needsApproval(view({ confidence: 0.5, status: 'RESOLVED' })), false);
  assert.equal(shouldAutoStart(view({ confidence: 0.9, status: 'RESOLVED' }), new Set()), false);
});

test('confidence is shown truncated at the 80% boundary', () => {
  assert.equal(formatConfidence(0.7999), '79.99%');
  assert.equal(formatConfidence(0.79999), '79.99%');
  assert.equal(formatConfidence(0.8), '80.00%');
  assert.equal(formatConfidence(0.8001), '80.01%');
  assert.equal(formatConfidence(1), '100.00%');
  for (const bad of [NaN, Infinity, -Infinity, null, undefined, '0.9']) assert.equal(formatConfidence(bad), '—');
});

test('a diff is shown only for a stored VALIDATED proposal', () => {
  assert.equal(canShowDiff(VALIDATED), true);
  for (const status of ['RUNNING', 'NOT_GENERATED', 'REJECTED', 'FAILED']) {
    assert.equal(canShowDiff({ ...VALIDATED, effective_status: status }), false, status);
  }
  assert.equal(canShowDiff({ effective_status: 'VALIDATED', unified_diff: null }), false);
  assert.equal(canShowDiff(null), false);
  assert.deepEqual(diffLines(VALIDATED.unified_diff).map((l) => l.kind), ['header', 'header', 'hunk', 'del', 'add']);
});

test('progress, retry and failure reasons', () => {
  assert.equal(isInProgress(view({ state: 'QUEUED' })), true);
  assert.equal(isInProgress(view({ state: 'COMPLETED', proposal: VALIDATED })), false);
  // The backend decides (FAILED, transient refusal, stale QUEUED); the page only follows retry_allowed.
  const withRetry = (v, allowed) => ({ ...v, routing: { ...v.routing, retry_allowed: allowed } });
  assert.equal(canRetry(withRetry(view({ state: 'COMPLETED', proposalStatus: 'FAILED' }), true)), true);
  assert.equal(canRetry(withRetry(view({ state: 'QUEUED' }), true)), true, 'stale queued job');
  assert.equal(canRetry(withRetry(view({ state: 'COMPLETED', proposalStatus: 'FAILED' }), false)), false);
  assert.equal(canRetry(view({ state: 'COMPLETED', proposalStatus: 'FAILED' })), false, 'missing flag = no retry');
  assert.equal(canRetry(withRetry(view({ status: 'RESOLVED' }), true)), false);
  assert.match(reasonText('LLM_UNAVAILABLE'), /could not be reached/);
  assert.match(reasonText('LLM_INVALID_CHOICE'), /outside the allowed menu/);
  assert.equal(reasonText('SOME_NEW_CODE'), 'some new code');
});

test('telemetry facts are kept apart from runbook guidance', () => {
  const ev = evidenceView({ evidence: {
    facts_offered: [{ id: 'F3', source: 'telemetry', text: 'memory peak 96.0Mi' }, { id: 'D1', source: 'diagnosis', text: 'confidence 0.90' }],
    fact_ids_cited: ['F3'], runbooks_offered: [{ runbook_id: 'rb', guidance: ['g'] }], runbook_ids_cited: ['rb'],
  } });
  assert.deepEqual(ev.incidentFacts.map((f) => [f.id, f.cited]), [['F3', true]]);
  assert.deepEqual(ev.diagnosisFacts.map((f) => f.id), ['D1']);
  assert.equal(ev.runbooks[0].cited, true);
});

test('API client: errors surface the backend reason; bodies never carry confidence', async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('/patch-proposals') && options?.method === 'POST') {
      return { ok: false, status: 403, json: async () => ({ detail: 'APPROVAL_REQUIRED: confidence 0.7999 < 0.80' }) };
    }
    if (url.endsWith('/approve')) return { ok: true, json: async () => ({ started: true, view: {} }) };
    return { ok: false, status: 500, json: async () => { throw new Error('not json'); } };
  };
  const { api } = await import('../lib/api.js');
  await assert.rejects(api.startPatchProposal('inc-1'), /APPROVAL_REQUIRED/);
  await assert.rejects(api.getPatchProposals('inc-1'), /API error 500/);
  await api.approvePatchGeneration('inc-1', 'ok');
  for (const call of calls.filter((c) => c.options?.body)) {
    const body = JSON.parse(call.options.body);
    assert.deepEqual(Object.keys(body).sort().filter((k) => !['retry', 'reason'].includes(k)), [], call.url);
  }
});

test('no UI action can apply or deploy a patch', () => {
  const panel = read('components/AIRemediationPanel.js');
  const used = new Set([...panel.matchAll(/api\.(\w+)\(/g)].map((m) => m[1]));
  assert.deepEqual([...used].sort(), ['approvePatchGeneration', 'getPatchProposals', 'rejectPatchGeneration', 'startPatchProposal']);
  assert.doesNotMatch(panel, /fetch\(/, 'all backend calls go through lib/api.js');
  const buttons = [...panel.matchAll(/<button[\s\S]*?<\/button>/g)].map((m) => m[0]);
  assert.ok(buttons.length >= 3);
  for (const b of buttons) assert.doesNotMatch(b, /apply|deploy|merge|commit|push|execute/i, b);
  const api = read('lib/api.js');
  const start = api.lastIndexOf('\n', api.indexOf('Stage 2'));
  const stage2 = api.slice(start, api.indexOf('// Metrics')).replace(/\/\/.*$/gm, '');
  assert.match(stage2, /patch-proposals/);
  assert.doesNotMatch(stage2, /apply|deploy|confidence|route/i, 'Stage 2 calls: code only, comments stripped');
  assert.match(read('app/incidents/[id]/page.js'), /<AIRemediationPanel /);
});
