import crypto from 'node:crypto';
import { constants, createReadStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';

export interface PrivateNarrationRetentionConfig {
  preserveNarration: boolean;
  nodeEnv: string | undefined;
  host: string | undefined;
}

export interface NarrationArchiveProvenance {
  render_preset_id: string;
  render_preset_sha256: string;
  voice_profile_sha256: string;
  month_bank_id: string;
  month_bank_sha256: string;
  join_pause_seconds: number;
}

export interface AssembledNarrationFile {
  ordinal: number;
  path: string;
}

export interface NarrationArchiveReceipt {
  schema_version: 1;
  archive_id: string;
  manifest_sha256: string;
  file_count: number;
}

const isExplicitLoopbackHost = (host: string | undefined): boolean =>
  host === 'localhost' || host === '127.0.0.1' || host === '::1';

const isOwnedDescendant = (root: string, candidate: string): boolean => {
  const relative = path.relative(root, candidate);
  return relative !== '' && !relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative);
};

const sha256File = async (filePath: string): Promise<string> => new Promise((resolve, reject) => {
  const hash = crypto.createHash('sha256');
  const stream = createReadStream(filePath);
  stream.on('error', reject);
  stream.on('data', (chunk) => hash.update(chunk));
  stream.on('end', () => resolve(hash.digest('hex')));
});

export const resolvePrivateNarrationRetention = ({
  preserveNarration,
  nodeEnv,
  host,
}: PrivateNarrationRetentionConfig): boolean => {
  if (!preserveNarration) return false;
  if (nodeEnv !== 'development' || !isExplicitLoopbackHost(host)) {
    throw new Error('REMOTION_PRESERVE_NARRATION=true requires NODE_ENV=development and an explicit loopback HOST');
  }
  return true;
};

const archiveJobId = (jobId: string): void => {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(jobId)) throw new Error('narration archive job id is invalid');
};

export const preserveSceneNarration = async ({
  enabled,
  outputDir,
  stagingDir,
  jobId,
  provenance,
  assembledWavs,
}: {
  enabled: boolean;
  outputDir: string;
  stagingDir: string;
  jobId: string;
  provenance: NarrationArchiveProvenance;
  assembledWavs: AssembledNarrationFile[];
}): Promise<NarrationArchiveReceipt | undefined> => {
  if (!enabled) return undefined;
  archiveJobId(jobId);
  if (assembledWavs.length !== 12 || assembledWavs.some((file, index) => file.ordinal !== index + 1)) {
    throw new Error('scene-v2 narration archive requires twelve ordered assembled WAVs');
  }

  const outputRoot = path.resolve(outputDir);
  const sourceRoot = path.resolve(stagingDir);
  const archiveRoot = path.resolve(outputRoot, 'narration-archive');
  const archiveDir = path.resolve(archiveRoot, jobId);
  if (!isOwnedDescendant(outputRoot, archiveRoot) || !isOwnedDescendant(archiveRoot, archiveDir)) {
    throw new Error('narration archive path is outside renderer output');
  }

  await fs.mkdir(archiveRoot, { recursive: true, mode: 0o700 });
  await fs.mkdir(archiveDir, { recursive: false, mode: 0o700 });
  try {
    const files = [] as Array<{ ordinal: number; filename: string; sha256: string; bytes: number }>;
    for (const source of assembledWavs) {
      const sourcePath = path.resolve(source.path);
      if (!isOwnedDescendant(sourceRoot, sourcePath)) throw new Error('assembled narration path is outside renderer staging');
      const filename = `${String(source.ordinal).padStart(2, '0')}-assembled.wav`;
      const destination = path.resolve(archiveDir, filename);
      if (!isOwnedDescendant(archiveDir, destination)) throw new Error('narration archive filename is invalid');
      const expectedSha256 = await sha256File(sourcePath);
      await fs.copyFile(sourcePath, destination, constants.COPYFILE_EXCL);
      await fs.chmod(destination, 0o600);
      const stat = await fs.stat(destination);
      const archivedSha256 = await sha256File(destination);
      if (archivedSha256 !== expectedSha256) throw new Error(`narration archive hash mismatch for ordinal ${source.ordinal}`);
      files.push({ ordinal: source.ordinal, filename, sha256: archivedSha256, bytes: stat.size });
    }
    const manifest = {
      schema_version: 1,
      archive_kind: 'scene-v2-narration',
      job_id: jobId,
      render_contract: 'scene-v2',
      ...provenance,
      assembled_wavs: files,
    };
    const manifestBytes = Buffer.from(`${JSON.stringify(manifest)}\n`, 'utf8');
    await fs.writeFile(path.join(archiveDir, 'provenance.json'), manifestBytes, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    return {
      schema_version: 1,
      archive_id: jobId,
      manifest_sha256: crypto.createHash('sha256').update(manifestBytes).digest('hex'),
      file_count: files.length,
    };
  } catch (error) {
    await fs.rm(archiveDir, { recursive: true, force: true });
    throw error;
  }
};
