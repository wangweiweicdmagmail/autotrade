'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { emaFromPeriods } = require('../service/ema.js');

test('EMA9 使用倒序 chart periods 并区分实时值与确认值', () => {
  const periods = Array.from({ length: 10 }, (_, i) => ({ close: 10 - i }));
  let expected0 = null;
  let expected1 = null;
  for (let close = 1; close <= 10; close++) {
    expected0 = expected0 === null ? close : 0.2 * close + 0.8 * expected0;
    if (close <= 9) expected1 = expected1 === null ? close : 0.2 * close + 0.8 * expected1;
  }
  assert.equal(emaFromPeriods(periods, 0, 9), expected0);
  assert.equal(emaFromPeriods(periods, 1, 9), expected1);
});

test('EMA9 数据不足时返回 null', () => {
  assert.equal(emaFromPeriods([], 0, 9), null);
  assert.equal(emaFromPeriods([{ close: 1 }], 1, 9), null);
});
