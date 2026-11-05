'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {chartRetryReason} = require('../service/chart_health');
const entry = {createdAt:1000, rebuilding:false, gaveUp:false};
test('重建无回报仍会到期再次检查，不因rebuilding标志永久跳过', () => {
  const rebuilding = {...entry,rebuilding:true};
  assert.equal(chartRetryReason(rebuilding,[],null,90000,90000),null);
  assert.equal(chartRetryReason(rebuilding,[],null,92000,90000),'无K线数据');
  assert.equal(chartRetryReason({...rebuilding,createdAt:92000},[],null,95000,90000),null);
  assert.equal(chartRetryReason({...rebuilding,gaveUp:true},[],null,999999,90000),null);
});
test('已有K线但确认指标缺失也需自愈，可选attack缺失不重建', () => {
  assert.equal(chartRetryReason(entry,[{time:10}],null,92000,90000),'确认趋势指标未返回');
  assert.equal(chartRetryReason(entry,[{time:10}],1e100,92000,90000),'确认趋势指标未返回');
  for (const value of [0,1,-1]) assert.equal(chartRetryReason(entry,[{time:10}],value,92000,90000),null);
});
