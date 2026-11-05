'use strict';

// A rebuild is an attempt with a deadline, not a permanent exemption from
// inspection. Optional studies (e.g. attack premarket fields) are excluded.
function chartRetryReason(entry, prices, confirmedTrend, now, graceMs) {
  if (entry.gaveUp || now - entry.createdAt <= graceMs) return null;
  if (!prices?.length) return '无K线数据';
  if (!Number.isFinite(confirmedTrend) || Math.abs(confirmedTrend) >= 1e99) {
    return '确认趋势指标未返回';
  }
  return null;
}
module.exports = { chartRetryReason };
