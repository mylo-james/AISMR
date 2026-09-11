/** Convert the environment string to the exact union Remotion accepts. */
export const parseFrameConcurrency = (raw) => {
  const value = String(raw ?? '1').trim();
  if (/^[1-9]\d*$/.test(value)) {
    const number = Number(value);
    if (Number.isSafeInteger(number)) return number;
  }
  if (/^(?:[1-9]\d?|100)%$/.test(value)) return value;
  throw new Error('REMOTION_FRAME_CONCURRENCY must be a positive integer (for example "1") or percentage from 1% through 100%');
};
