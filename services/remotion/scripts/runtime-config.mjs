export const parsePositiveInteger = (raw, variableName) => {
  const value = String(raw ?? '').trim();
  if (!/^[1-9]\d*$/.test(value)) {
    throw new Error(`${variableName} must be a finite positive integer`);
  }
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1) {
    throw new Error(`${variableName} must be a finite positive integer`);
  }
  return number;
};
