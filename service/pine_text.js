/**
 * pine_text.js — 指标加密正文注入（2026-10-27 TV 服务端变更的修复）
 *
 * 背景：TradingView 当天起不再在 translate 响应里返回 result.ilTemplate，
 * create_study 必须携带脚本加密正文 text，否则所有 study（含内置）报
 * "Can't parse pine"。正文仍在 translate 响应里，只是挪到了
 * metaInfo.inputs 中 id="text" 的 defval（见上游 TradingView-API issue #354）。
 *
 * 本模块在加载指标时直接从 translate 响应提取 text 并用库导出的
 * PineIndicator 重建实例注入——每次启动都是最新正文，不依赖任何本地缓存，
 * 也不修改 node_modules（重装依赖零影响）。translate 提取失败时回退
 * pine_texts.json 缓存（由 refresh_pine_texts.py 生成）。
 */

'use strict';

const fs = require('fs');
const path = require('path');

function extractTextFromTranslate(result) {
  if (result.ilTemplate) return result.ilTemplate;
  const inputs = (result.metaInfo && result.metaInfo.inputs) || [];
  const text = inputs.find((i) => i.id === 'text');
  if (text && typeof text.defval === 'string' && text.defval) return text.defval;
  return null;
}

async function fetchScriptText(id, creds) {
  const url = `https://pine-facade.tradingview.com/pine-facade/translate/${id.replace(/ |%/g, '%25')}/last`;
  let lastErr = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = await fetch(url, {
        headers: { cookie: `sessionid=${creds.sessionid};sessionid_sign=${creds.sessionid_sign}` },
      });
      const data = await resp.json();
      if (!data.success || !data.result) throw new Error(`translate 失败: ${data.reason || resp.status}`);
      const text = extractTextFromTranslate(data.result);
      if (text) return text;
      throw new Error('translate 响应无 text defval');
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 1000 * (2 ** attempt)));
    }
  }
  throw lastErr;
}

function loadTextCache() {
  try {
    const cachePath = process.env.AUTOTRADE_PINE_VALIDATION_CACHE || path.join(__dirname, 'pine_texts.json');
    return JSON.parse(fs.readFileSync(cachePath, 'utf8'));
  } catch (e) {
    return {};
  }
}

function sameVersion(a, b) {
  const normalize = (v) => {
    const parts = String(v == null ? '' : v).trim().replace(/^v/i, '').split('.');
    while (parts.length > 1 && parts[parts.length - 1] === '0') parts.pop();
    return parts.join('.');
  };
  const left = normalize(a);
  return left !== '' && left === normalize(b);
}

/**
 * getIndicator + 加密正文注入。缓存正文必须与远端版本一致，禁止把旧脚本
 * 拼到新元数据上（会静默丢 plot）。options 仅供离线测试注入正文与缓存。
 */
async function loadIndicatorWithText(TradingView, id, creds, options = {}) {
  const indic = await TradingView.getIndicator(id, 'last', creds.sessionid, creds.sessionid_sign);
  const fetchText = options.fetchText || fetchScriptText;
  const cache = options.cache || loadTextCache();
  let text = null;
  let fallbackError = '';
  try {
    text = await fetchText(id, creds);
  } catch (e) {
    const reason = (e.message || e).toString().slice(0, 120);
    const cached = cache[id];
    if (cached && cached.text && sameVersion(cached.version, indic.pineVersion)) {
      text = cached.text;
      console.error(`[pine_text] ${id} translate 提取失败，回退同版本缓存正文 v${indic.pineVersion}: ${reason}`);
    } else if (cached && cached.text) {
      fallbackError = `缓存正文 v${cached.version || '未知'} 与远端 v${indic.pineVersion} 不一致`;
    } else {
      fallbackError = `translate 提取失败且无缓存: ${reason}`;
    }
  }
  if (!text) {
    const error = new Error(`[pine_text] ${id} 无匹配正文（远端 v${indic.pineVersion}）: ${fallbackError}`);
    error.cacheIssue = {
      cached_version: cache[id]?.text ? (cache[id].version || '未知') : null,
      remote_version: indic.pineVersion,
    };
    throw error;
  }
  return new TradingView.PineIndicator({
    pineId: indic.pineId, pineVersion: indic.pineVersion,
    description: indic.description, shortDescription: indic.shortDescription,
    inputs: indic.inputs, plots: indic.plots, script: text,
  });
}

module.exports = { loadIndicatorWithText, fetchScriptText };
