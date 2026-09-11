import {readFileSync} from 'node:fs';

// The deployment API includes projectId and meta; `vercel inspect --json` omits them.
const candidate = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const {DEPLOYMENT_ID, VERCEL_PROJECT_ID, SOURCE_SHA, CONFIG_REVISION} = process.env;
if (!/^dpl_[A-Za-z0-9]+$/.test(DEPLOYMENT_ID ?? '') ||
    !/^prj_[A-Za-z0-9]+$/.test(VERCEL_PROJECT_ID ?? '') ||
    !/^[0-9a-f]{40}$/.test(SOURCE_SHA ?? '') ||
    !/^[A-Za-z0-9._-]{1,128}$/.test(CONFIG_REVISION ?? '') ||
    candidate?.id !== DEPLOYMENT_ID ||
    candidate?.projectId !== VERCEL_PROJECT_ID ||
    candidate?.readyState !== 'READY' ||
    candidate?.target !== 'production' ||
    candidate?.meta?.deploymentSourceSha !== SOURCE_SHA ||
    candidate?.meta?.deploymentConfigRevision !== CONFIG_REVISION) {
  throw new Error('Staged candidate identity differs');
}
console.log(`Verified staged candidate ${DEPLOYMENT_ID}`);
