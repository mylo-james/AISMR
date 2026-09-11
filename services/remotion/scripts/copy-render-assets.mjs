import path from 'node:path';
import { cp, access, constants, copyFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceTemplates = path.join(root, 'templates');
const compiledTemplates = path.join(root, 'dist', 'templates');
const sourceScripts = path.join(root, 'scripts');
const compiledScripts = path.join(root, 'dist', 'scripts');
const sourceVoiceAssets = path.resolve(root, '..', '..', 'data', 'media', 'voice');
const compiledVoiceAssets = path.join(root, 'dist', 'data', 'media', 'voice');
await copyFile(path.join(root, 'render.mjs'), path.join(root, 'dist', 'render.mjs'));
await cp(sourceTemplates, compiledTemplates, { recursive: true, force: true });
await cp(sourceScripts, compiledScripts, { recursive: true, force: true });
await cp(
  path.join(sourceVoiceAssets, 'months', 'teacup-whisper-months-v1'),
  path.join(compiledVoiceAssets, 'months', 'teacup-whisper-months-v1'),
  { recursive: true, force: true },
);
await mkdir(path.join(compiledVoiceAssets, 'presets'), { recursive: true });
await mkdir(path.join(compiledVoiceAssets, 'profiles'), { recursive: true });
await copyFile(
  path.join(sourceVoiceAssets, 'presets', 'monthly-saved-voice-v2.json'),
  path.join(compiledVoiceAssets, 'presets', 'monthly-saved-voice-v2.json'),
);
await copyFile(
  path.join(sourceVoiceAssets, 'profiles', 'eleven-v3-whisper-en-v1.json'),
  path.join(compiledVoiceAssets, 'profiles', 'eleven-v3-whisper-en-v1.json'),
);
await access(path.join(compiledScripts, 'frame-concurrency.mjs'), constants.R_OK);
await access(path.join(compiledScripts, 'runtime-config.mjs'), constants.R_OK);
for (const name of ['aismr.tsx', 'motivational.tsx', 'monthly.tsx']) {
  await access(path.join(compiledTemplates, name), constants.R_OK);
}
await access(path.join(compiledVoiceAssets, 'months', 'teacup-whisper-months-v1', 'manifest.json'), constants.R_OK);
await access(path.join(compiledVoiceAssets, 'presets', 'monthly-saved-voice-v2.json'), constants.R_OK);
