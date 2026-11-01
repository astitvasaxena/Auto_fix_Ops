import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_TARGET_APP_URL, targetAppLinks, targetAppStatus } from '../lib/targetApp.mjs';

test('default links point at the browser-access port', () => {
  assert.equal(DEFAULT_TARGET_APP_URL, 'http://localhost:30081');
  for (const configured of [undefined, '', '   ']) {
    assert.deepEqual(targetAppLinks(configured), {
      base: 'http://localhost:30081', host: 'localhost:30081', port: '30081',
      app: 'http://localhost:30081/docs', health: 'http://localhost:30081/health', ready: 'http://localhost:30081/ready',
    });
  }
});

test('every link is derived from the one configured base URL', () => {
  const l = targetAppLinks('https://target.example:8443/demo/');
  assert.equal(l.base, 'https://target.example:8443/demo');
  assert.equal(l.health, 'https://target.example:8443/demo/health');
  assert.equal(l.ready, 'https://target.example:8443/demo/ready');
  assert.equal(l.app, 'https://target.example:8443/demo/docs');
  assert.equal(targetAppLinks('http://127.0.0.1:30091/').health, 'http://127.0.0.1:30091/health');
  assert.equal(targetAppLinks('http://localhost').port, '80');
});

test('anything but a plain http(s) URL falls back to the default', () => {
  for (const bad of ['javascript:alert(1)', 'not a url', 'file:///C:/x', 'ftp://host/']) {
    assert.equal(targetAppLinks(bad).base, DEFAULT_TARGET_APP_URL, bad);
  }
});

test('status comes from Kubernetes and never claims health without ready pods', () => {
  assert.equal(targetAppStatus(null).label, 'Unknown');
  assert.equal(targetAppStatus({ available: false }).label, 'Unknown');
  assert.equal(targetAppStatus({ available: true, summary: { pods: 1, ready_pods: 1 } }).label, 'Healthy');
  assert.equal(targetAppStatus({ available: true, summary: { pods: 1, ready_pods: 0 } }).label, 'Not Ready');
  assert.equal(targetAppStatus({ available: true, summary: { pods: 1, ready_pods: 0, crash_loop: true } }).label, 'Unreachable');
  assert.equal(targetAppStatus({ available: true, summary: { pods: 0, ready_pods: 0 } }).label, 'Unreachable');
  assert.equal(targetAppStatus({ available: true, summary: { pods: 2, ready_pods: 1 } }).label, 'Not Ready');
});
