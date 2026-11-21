'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { createRequire } = require('node:module');

const AUTH_URL = 'https://www.tradingview.com/';
const AUTH_HOSTS = new Set(['tradingview.com', 'www.tradingview.com', 'cn.tradingview.com']);
const LOCAL_CONFIG = path.join(__dirname, 'tv_market_proxy.local.json');

function readProxy() {
  let value = process.env.TV_MARKET_PROXY;
  if (value === undefined) {
    try {
      value = JSON.parse(fs.readFileSync(LOCAL_CONFIG, 'utf8')).proxy;
    } catch (error) {
      if (error.code !== 'ENOENT') throw new Error('无法读取 TradingView 行情代理配置');
    }
  }
  if (!value) return null;

  const proxy = URL.canParse(value) ? new URL(value) : null;
  if (!proxy || !['http:', 'https:'].includes(proxy.protocol) || proxy.username || proxy.password ||
      proxy.pathname !== '/' || proxy.search || proxy.hash) {
    throw new Error('TradingView 行情代理须填写不带账号和路径的 HTTP(S) 地址');
  }
  return proxy;
}

function requestAuthPage(url, proxy, cookie) {
  return new Promise((resolve, reject) => {
    // Keep session cookies inside the TLS request and off the process command line.
    const child = execFile('curl', [
      '--disable', '--config', '-', '--silent', '--noproxy', '', '--proxy', proxy.href,
      '--connect-timeout', '4', '--max-time', '8', '--write-out', '\n%{http_code}\n%{redirect_url}',
      url.href,
    ], {maxBuffer: 12 * 1024 * 1024}, (error, stdout) => {
      if (error) {
        const detail = error.code === 28 ? '超时' : '传输失败';
        reject(new Error(`TradingView 行情授权经代理请求${detail}`));
        return;
      }
      const trailer = /\n(\d{3})\n([^\n]*)$/.exec(stdout);
      if (!trailer) {
        reject(new Error('TradingView 行情授权响应不完整'));
        return;
      }
      resolve({status: Number(trailer[1]), next: trailer[2], html: stdout.slice(0, trailer.index)});
    });
    child.stdin.on('error', () => {}); // execFile reports startup and transport errors above.
    child.stdin.end(`header = ${JSON.stringify(cookie)}\n`);
  });
}

async function getAuthToken(proxy, credentials) {
  if (!credentials.sessionid || !credentials.sessionid_sign) {
    throw new Error('TradingView 行情登录凭证缺少必要字段');
  }
  const cookie = `Cookie: sessionid=${credentials.sessionid}; sessionid_sign=${credentials.sessionid_sign}`;
  let url = new URL(AUTH_URL);
  for (let count = 0; count < 3; count++) {
    const {status, next, html} = await requestAuthPage(url, proxy, cookie);
    if (status === 200) {
      const token = /"auth_token"\s*:\s*"([^"]+)"/.exec(html)?.[1];
      if (token && token !== 'unauthorized_user_token') return token;
      throw new Error('TradingView 登录页没有返回已登录的行情授权');
    }
    if (status < 300 || status >= 400 || !next) {
      throw new Error(`TradingView 行情授权请求返回 HTTP ${status}`);
    }
    const redirect = new URL(next, url);
    if (!AUTH_HOSTS.has(redirect.hostname) || redirect.protocol !== 'https:' || redirect.username || redirect.password || redirect.port) {
      throw new Error('TradingView 行情授权重定向到未受信任的地址');
    }
    url = redirect;
  }
  throw new Error('TradingView 行情授权重定向次数过多');
}

async function createMarketClient(TradingView, credentials) {
  const options = {token: credentials.sessionid, signature: credentials.sessionid_sign};
  const proxy = readProxy();
  if (!proxy) return new TradingView.Client(options);

  const authToken = await getAuthToken(proxy, credentials);
  const {HttpsProxyAgent} = require('https-proxy-agent');
  const requireFromTV = createRequire(require.resolve('@mathieuc/tradingview'));
  const agent = new HttpsProxyAgent(proxy.href);
  const clientPath = requireFromTV.resolve('./src/client');
  const originalClientModule = require.cache[clientPath];
  const WebSocket = requireFromTV('ws');
  const socketModule = require.cache[requireFromTV.resolve('ws')];
  const requests = requireFromTV('./src/miscRequests');
  const originalGetUser = requests.getUser;

  // TradingView-API v3 takes no WebSocket agent option. Load a private copy of
  // its client class with a proxied WebSocket constructor and restore the module cache.
  try {
    socketModule.exports = class extends WebSocket {
      constructor(address, options) {
        super(address, {...options, agent, handshakeTimeout: 12_000});
      }
    };
    delete require.cache[clientPath];
    const Client = requireFromTV(clientPath);
    requests.getUser = async () => ({authToken});
    const client = new Client(options);
    console.error(`[connection] TradingView 行情代理：${proxy.origin}`);
    return client;
  } finally {
    socketModule.exports = WebSocket;
    require.cache[clientPath] = originalClientModule;
    requests.getUser = originalGetUser;
  }
}

module.exports = {createMarketClient};
