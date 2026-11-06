'use strict';

// An unanswered chart request can expire even if it was itself a rebuild.
function chartRetryReason(entry, bar, now, graceMs) {
  if (entry.gaveUp || now - entry.createdAt <= graceMs || bar) return null;
  return '无K线数据';
}

function studyRetryReason(slot, barTime, studyTime, now, graceMs) {
  if (studyTime === barTime) {
    slot.lagSince = null;
    slot.failures = 0;
    return null;
  }
  slot.lagSince ??= now;
  return now - slot.lagSince >= graceMs ? '指标未返回当前K线的输出' : null;
}

function retryDelay(failures) {
  return Math.min(120000, 5000 * 2 ** Math.min(failures - 1, 5));
}

module.exports = { chartRetryReason, studyRetryReason, retryDelay };
