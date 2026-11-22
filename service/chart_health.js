'use strict';

// An unanswered chart request can expire even if it was itself a rebuild.
function chartRetryReason(entry, bar, now, graceMs) {
  if (entry.gaveUp || now - entry.createdAt <= graceMs || bar) return null;
  return '无K线数据';
}

function retryDelay(failures) {
  return Math.min(120000, 5000 * 2 ** Math.min(failures - 1, 5));
}

module.exports = { chartRetryReason, retryDelay };
