/**
 * @param {Storage} storage
 * @param {string} key
 * @param {number} nowMs
 * @param {number} ttlMs
 */
export function readSnapshotCache(storage, key, nowMs, ttlMs) {
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (!value || !value.snapshot || !Number.isFinite(value.cachedAt)) return null;
    return {
      snapshot: value.snapshot,
      cachedAt: value.cachedAt,
      isFresh: nowMs - value.cachedAt <= ttlMs,
    };
  } catch {
    return null;
  }
}

/** @param {Storage} storage */
export function writeSnapshotCache(storage, key, snapshot, nowMs) {
  try {
    storage.setItem(key, JSON.stringify({ cachedAt: nowMs, snapshot }));
    return true;
  } catch {
    return false;
  }
}

