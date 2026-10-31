import { test } from 'node:test';
import assert from 'node:assert/strict';
import { STAGES, pipelineStates, runSteps, blockedText, stabilityProgress } from '../lib/pipeline.mjs';

const at = (key) => STAGES.findIndex((s) => s.key === key);
const tl = (...statuses) => statuses.map((status) => ({ status, timestamp: '2026-10-31T00:00:00' }));

test('nothing is completed without an incident', () => {
  assert.ok(pipelineStates(null).every((s) => s === 'idle'));
});

test('alertmanager incident at DIAGNOSED: diagnosis done, policy active, later stages idle', () => {
  const s = pipelineStates({ status: 'DIAGNOSED', source: 'alertmanager', status_timeline: tl('CONTEXT_BUILT', 'DIAGNOSED') });
  assert.deepEqual(s.slice(0, at('diagnosis') + 1), Array(at('diagnosis') + 1).fill('completed'));
  assert.equal(s[at('policy')], 'active');
  assert.equal(s[at('remediation')], 'idle');
  assert.equal(s[at('verification')], 'idle');
});

test('chaos-injected incidents never claim Prometheus/AlertManager ran', () => {
  const s = pipelineStates({ status: 'INGESTED', source: 'chaos_injection', status_timeline: [] });
  assert.equal(s[at('prometheus')], 'skipped');
  assert.equal(s[at('alertmanager')], 'skipped');
  assert.equal(s[at('api')], 'completed');
  assert.equal(s[at('context')], 'active');
});

test('failure is shown where it happened, not as a finished pipeline', () => {
  const s = pipelineStates({ status: 'FAILED', source: 'alertmanager', status_timeline: tl('CONTEXT_BUILT', 'FAILED') });
  assert.equal(s[at('diagnosis')], 'failed');
  assert.equal(s[at('policy')], 'idle');
  assert.equal(s[at('verification')], 'idle');
});

test('PENDING_PR_MERGE alone does not mark remediation completed; a deployed run does', () => {
  const inc = { status: 'PENDING_PR_MERGE', source: 'alertmanager',
    status_timeline: tl('CONTEXT_BUILT', 'DIAGNOSED', 'POLICY_APPROVED', 'REMEDIATING', 'PENDING_PR_MERGE') };
  assert.equal(pipelineStates(inc)[at('remediation')], 'active');
  assert.equal(pipelineStates(inc, { stage: 'PR_OPEN' })[at('remediation')], 'active');
  const s = pipelineStates(inc, { stage: 'VERIFYING' });
  assert.equal(s[at('remediation')], 'completed');
  assert.equal(s[at('verification')], 'active');
});

test('verified resolution completes every stage; a manual close does not', () => {
  const full = pipelineStates({ status: 'RESOLVED', source: 'alertmanager',
    status_timeline: tl('CONTEXT_BUILT', 'DIAGNOSED', 'POLICY_APPROVED', 'REMEDIATING', 'PENDING_PR_MERGE', 'VERIFIED', 'RESOLVED') });
  assert.ok(full.every((x) => x === 'completed'));
  const manual = pipelineStates({ status: 'RESOLVED', source: 'alertmanager',
    status_timeline: tl('CONTEXT_BUILT', 'DIAGNOSED', 'ESCALATED', 'RESOLVED') });
  assert.equal(manual[at('policy')], 'warning');
  assert.equal(manual[at('remediation')], 'skipped');
  assert.equal(manual[at('verification')], 'skipped');
});

test('run steps follow backend evidence only', () => {
  assert.ok(runSteps(null).every((s) => s.state === 'idle'));
  const blocked = runSteps({ stage: 'APPROVED', blocked_code: 'GITHUB_NOT_CONFIGURED', final: false, github: null });
  assert.deepEqual(blocked.map((s) => s.state), ['completed', 'warning', 'idle', 'idle', 'idle']);
  const merged = runSteps({ stage: 'READY_TO_DEPLOY', final: false,
    github: { pr_number: 4, merged: true, merged_content_matches: true } });
  assert.deepEqual(merged.map((s) => s.state), ['completed', 'completed', 'completed', 'active', 'idle']);
  const rolledBack = runSteps({ stage: 'ROLLED_BACK', final: true, github: { mode: 'skipped' },
    deployment: { patched: true, rollout: { returncode: 0 } }, verification: { result: 'FAILED' } });
  assert.deepEqual(rolledBack.map((s) => s.state), ['completed', 'skipped', 'skipped', 'completed', 'failed']);
  const rejected = runSteps({ stage: 'REJECTED', final: true });
  assert.equal(rejected[0].state, 'failed');
});

test('blocked reasons are explained and stability progress is bounded', () => {
  assert.match(blockedText('DEPLOY_DISABLED'), /DEPLOY_MODE=kubectl/);
  assert.equal(blockedText(null), '');
  const run = { verification: { stable_since: '2026-10-31T00:00:00Z', stabilization_s: 60 } };
  assert.equal(stabilityProgress(run, Date.parse('2026-10-31T00:00:30Z')), 0.5);
  assert.equal(stabilityProgress(run, Date.parse('2026-10-31T00:05:00Z')), 1);
  assert.equal(stabilityProgress({}), 0);
});

test('chaos mode and escalated remediation are shown from backend data only', async () => {
  const { chaosMode, CHAOS_FAULTS } = await import('../lib/pipeline.mjs');
  assert.equal(chaosMode(null), 'local');
  assert.equal(chaosMode({ chaos_enabled: true, evidence: { available: false } }), 'local');
  assert.equal(chaosMode({ chaos_enabled: true, evidence: { available: true } }), 'kubernetes');
  assert.deepEqual(CHAOS_FAULTS.map((f) => f.id), ['memory_leak', 'cpu_spike', 'crash_loop']);
  const inc = { status: 'ESCALATED', source: 'alertmanager',
    status_timeline: tl('CONTEXT_BUILT', 'DIAGNOSED', 'POLICY_APPROVED', 'REMEDIATING', 'PENDING_PR_MERGE', 'ESCALATED') };
  const rolledBack = pipelineStates(inc, { stage: 'ROLLED_BACK', final: true, deployment: { patched: true } });
  assert.equal(rolledBack[at('remediation')], 'completed');
  assert.equal(rolledBack[at('verification')], 'failed');
  const noPr = pipelineStates(inc, { stage: 'FAILED', final: true, deployment: null });
  assert.equal(noPr[at('remediation')], 'failed');
  assert.equal(pipelineStates({ ...inc, status_timeline: tl('DIAGNOSED', 'ESCALATED') })[at('policy')], 'warning');
});

test('Kubernetes target badge and CI status come only from backend data', async () => {
  const { podHealth, ciInfo } = await import('../lib/pipeline.mjs');
  assert.equal(podHealth(null).label, 'UNKNOWN');
  assert.equal(podHealth({ available: false }).label, 'UNKNOWN');
  assert.equal(podHealth({ available: true, summary: { crash_loop: true, pods: 1, ready_pods: 0 } }).label, 'CRASHLOOPBACKOFF');
  assert.equal(podHealth({ available: true, summary: { crash_loop: false, pods: 1, ready_pods: 1 } }).label, 'HEALTHY');
  assert.equal(podHealth({ available: true, summary: { crash_loop: false, pods: 1, ready_pods: 0 } }).label, 'NOT READY');
  assert.equal(ciInfo({}), null);
  assert.equal(ciInfo({ ci_status: 'failure' }).icon, '❌');
  assert.equal(ciInfo({ ci_status: 'success' }).label, 'success');
});
