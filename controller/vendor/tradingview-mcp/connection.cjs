/**
 * CDP connection helper for TradingView Desktop.
 *
 * Adapted from tradesdontlie/tradingview-mcp (src/connection.js)
 * https://github.com/tradesdontlie/tradingview-mcp
 * License: MIT (see LICENSE in this directory).
 * Converted from ESM to CommonJS; trimmed to the pieces this project needs.
 */
'use strict';

const CDP = require('chrome-remote-interface');

// Note: on this macOS setup, Python urllib must use "localhost" (127.0.0.1 is
// refused by the TV client); Node fetch/CDP accepts both. Default kept as
// 127.0.0.1 like upstream; override via TV_CDP_HOST=localhost if needed.
const CDP_HOST = process.env.TV_CDP_HOST || process.env.CDP_HOST || '127.0.0.1';
const CDP_PORT = Number(process.env.TV_CDP_PORT || process.env.CDP_PORT) || 9222;
const MAX_RETRIES = 5;
const BASE_DELAY = 500;

let client = null;
let targetInfo = null;

/** Escape a string for safe interpolation into JS evaluated via CDP. */
function safeString(str) {
  return JSON.stringify(String(str));
}

async function listTargets() {
  const resp = await fetch(`http://${CDP_HOST}:${CDP_PORT}/json/list`);
  return resp.json();
}

async function findChartTarget() {
  const targets = await listTargets();
  return targets.find((t) => t.type === 'page' && /tradingview\.com\/chart/i.test(t.url))
    || targets.find((t) => t.type === 'page' && /tradingview/i.test(t.url))
    || null;
}

async function connect() {
  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      const target = await findChartTarget();
      if (!target) {
        throw new Error('No TradingView chart target found. Is TradingView open with a chart?');
      }
      targetInfo = target;
      client = await CDP({ host: CDP_HOST, port: CDP_PORT, target: target.id });
      await client.Runtime.enable();
      await client.Page.enable();
      return client;
    } catch (err) {
      lastError = err;
      const delay = Math.min(BASE_DELAY * Math.pow(2, attempt), 30000);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw new Error(`CDP connection failed after ${MAX_RETRIES} attempts: ${lastError && lastError.message}`);
}

async function getClient() {
  if (client) {
    // 存活探测也必须有超时：连接僵死但 socket 未断时，
    // 无超时的 evaluate 会永久挂起（二次开发修复点）
    try {
      await withTimeout(
        client.Runtime.evaluate({ expression: '1', returnByValue: true }),
        5000, 'liveness check timeout');
      return client;
    } catch {
      try { await disconnect(); } catch { /* ignore */ }
    }
  }
  return connect();
}

/** 为 CDP 调用包裹超时（chrome-remote-interface 无内置超时）。 */
function withTimeout(promise, ms, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function evaluate(expression, opts = {}) {
  const timeoutMs = opts.timeoutMs || 15000;
  delete opts.timeoutMs;
  let lastError;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const c = await getClient();
      const result = await withTimeout(
        c.Runtime.evaluate({
          expression,
          returnByValue: true,
          awaitPromise: opts.awaitPromise ?? false,
          ...opts,
        }),
        timeoutMs, 'CDP evaluate timeout');
      if (result.exceptionDetails) {
        const msg = (result.exceptionDetails.exception && result.exceptionDetails.exception.description)
          || result.exceptionDetails.text
          || 'Unknown evaluation error';
        throw new Error(`JS evaluation error: ${msg}`);
      }
      return result.result ? result.result.value : undefined;
    } catch (err) {
      lastError = err;
      // 超时/断连：销毁连接，重建后重试一次
      try { await disconnect(); } catch { /* ignore */ }
    }
  }
  throw lastError;
}

async function evaluateAsync(expression) {
  return evaluate(expression, { awaitPromise: true });
}

async function disconnect() {
  if (client) {
    try { await client.close(); } catch { /* already gone */ }
    client = null;
    targetInfo = null;
  }
}

module.exports = {
  CDP_HOST,
  CDP_PORT,
  safeString,
  getClient,
  evaluate,
  evaluateAsync,
  disconnect,
  getTargetInfo: () => targetInfo,
};
