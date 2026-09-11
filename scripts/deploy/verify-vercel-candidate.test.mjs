import assert from 'node:assert/strict';
import {mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';

const script = fileURLToPath(new URL('./verify-vercel-candidate.mjs', import.meta.url));
const expected = {
  DEPLOYMENT_ID: 'dpl_candidate', VERCEL_PROJECT_ID: 'prj_aismr',
  SOURCE_SHA: '1'.repeat(40), CONFIG_REVISION: 'recorded-vnc-test',
};
const candidate = {
  id: expected.DEPLOYMENT_ID, projectId: expected.VERCEL_PROJECT_ID,
  target: 'production', readyState: 'READY',
  meta: {deploymentSourceSha: expected.SOURCE_SHA, deploymentConfigRevision: expected.CONFIG_REVISION},
};

function verify(data, overrides = {}) {
  const root = mkdtempSync(join(tmpdir(), 'aismr-candidate-test-'));
  try {
    const file = join(root, 'candidate.json');
    writeFileSync(file, JSON.stringify(data));
    return spawnSync(process.execPath, [script, file], {
      env: {...process.env, ...expected, ...overrides}, encoding: 'utf8',
    });
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
}

test('accepts the native deployment API identity and the exact reviewed source', () => {
  assert.equal(verify(candidate).status, 0);
});

test('rejects another deployment, project, source, configuration, target or state', () => {
  for (const data of [
    {...candidate, id: 'dpl_other'}, {...candidate, projectId: 'prj_other'},
    {...candidate, target: 'preview'}, {...candidate, readyState: 'BUILDING'},
    {...candidate, meta: {...candidate.meta, deploymentSourceSha: '2'.repeat(40)}},
    {...candidate, meta: {...candidate.meta, deploymentConfigRevision: 'changed'}},
    {...candidate, meta: undefined}, {...candidate, projectId: undefined}, null,
  ]) assert.notEqual(verify(data).status, 0);
});

test('fails closed when required expected identity is absent or malformed', () => {
  for (const key of Object.keys(expected)) assert.notEqual(verify(candidate, {[key]: ''}).status, 0);
  assert.notEqual(verify(candidate, {SOURCE_SHA: 'main'}).status, 0);
});
