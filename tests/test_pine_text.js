'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadIndicatorWithText } = require('../service/pine_text.js');

function fakeTradingView(version = '16') {
  function PineIndicator(options) {
    Object.assign(this, options);
  }
  return {
    PineIndicator,
    async getIndicator() {
      return {
        pineId: 'USER;test', pineVersion: version,
        description: 'test', shortDescription: 'test', inputs: {}, plots: {},
      };
    },
  };
}

test('实时正文优先，不受旧缓存影响', async () => {
  const indic = await loadIndicatorWithText(fakeTradingView(), 'ID', {}, {
    fetchText: async () => 'live-text',
    cache: { ID: { version: '14.0', text: 'old-text' } },
  });
  assert.equal(indic.script, 'live-text');
});

test('同版本缓存允许回退并规范化版本号', async () => {
  const indic = await loadIndicatorWithText(fakeTradingView('16'), 'ID', {}, {
    fetchText: async () => { throw new Error('empty text'); },
    cache: { ID: { version: '16.0', text: 'cached-text' } },
  });
  assert.equal(indic.script, 'cached-text');
});

test('版本错配时拒绝拼接旧正文', async () => {
  await assert.rejects(
    loadIndicatorWithText(fakeTradingView('16'), 'ID', {}, {
      fetchText: async () => { throw new Error('empty text'); },
      cache: { ID: { version: '14.0', text: 'old-text' } },
    }),
    /缓存正文 v14\.0 与远端 v16 不一致/,
  );
});

test('无正文且无缓存时明确失败', async () => {
  await assert.rejects(
    loadIndicatorWithText(fakeTradingView(), 'ID', {}, {
      fetchText: async () => { throw new Error('empty text'); },
      cache: {},
    }),
    /无匹配正文/,
  );
});
