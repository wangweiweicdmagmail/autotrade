'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadingPage() {
  class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.style = {}; this.hidden = false; }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(' '); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.text = ''; this.children = children; }
    setAttribute(key, value) { this[key] = value; }
  }
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  get('cache-panel').hidden = true;
  const context = vm.createContext({document: {getElementById: get, createElement: tag => new Element(tag)},
    fetch: () => new Promise(() => {}), setTimeout: () => {}, location: {replace() {}}, console});
  const html = fs.readFileSync(path.join(__dirname, '../console/static/loading.html'), 'utf8');
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  return {get, context, render(data) { context.input = data; vm.runInContext('render(input)', context); }};
}

function snapshot(required = true, local = true) {
  return {ready: false, elapsed: 300, tv_login: {required, local_actions: local,
    state: required ? 'login_required' : 'configured', detail: '等待专用 Chrome 登录'},
    stages: [{key: 'tv_login', label: 'TradingView 登录', ready: !required},
      {key: 'market', label: '行情与指标', ready: false,
        detail: required ? '等待登录完成后开始订阅' : '确认指标 0/54'}],
    market_progress: {total: 54, confirmed: 0, bars_ready: 0, subscribed: 0},
    market_pending: required ? [] : ['NASDAQ:QQQ']};
}

test('first-run login shows actionable guidance instead of an unexplained 0/54', () => {
  const page = loadingPage();
  page.render(snapshot());
  assert.equal(page.get('tv-login-panel').hidden, false);
  assert.match(page.get('heading').textContent, /TradingView 登录/);
  assert.match(page.get('status-text').textContent, /登录后自动继续/);
  assert.doesNotMatch(page.get('stages').textContent, /0\/54|等待标的/);
  assert.equal(page.get('tv-login-open').hidden, false);
  page.render(snapshot(true, false));
  assert.equal(page.get('tv-login-open').hidden, true);
});

test('after login the same page resumes actual market loading progress', () => {
  const page = loadingPage();
  page.render(snapshot());
  const data = snapshot(false);
  data.market_progress.confirmed = 12;
  data.stages[1].detail = '确认指标 12/54';
  page.render(data);
  assert.equal(page.get('tv-login-panel').hidden, true);
  assert.match(page.get('stages').textContent, /12\/54/);
  assert.match(page.get('stages').textContent, /等待标的：QQQ/);
  assert.doesNotMatch(page.get('heading').textContent, /请先/);
});

test('login button requests the local helper and never sends browser cookies', async () => {
  const page = loadingPage();
  page.render(snapshot());
  const requests = [];
  page.context.fetch = async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => ({ok: true})};
  };
  await page.get('tv-login-open').onclick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/startup/tv-login');
  assert.equal(requests[0].options.method, 'POST');
  assert.equal(requests[0].options.headers['X-Autotrade-Action'], 'tv-login');
  assert.equal(requests[0].options.body, undefined);
  assert.match(page.get('tv-login-result').textContent, /自动继续/);
  page.context.fetch = async () => ({ok: false, json: async () => ({detail: '请在服务器电脑登录'})});
  await page.get('tv-login-open').onclick();
  assert.equal(page.get('tv-login-result').textContent, '请在服务器电脑登录');
  assert.equal(page.get('tv-login-open').disabled, false);
});

test('a save error stays visible after opening login and does not ask the user to log in again', async () => {
  const page = loadingPage();
  page.render(snapshot());
  page.context.fetch = async () => ({ok: true, json: async () => ({ok: true})});
  await page.get('tv-login-open').onclick();
  const data = snapshot();
  data.tv_login.state = 'save_error';
  data.tv_login.detail = '登录已检测到，但凭据保存失败；请检查写入权限。';
  page.render(data);
  assert.match(page.get('tv-login-result').textContent, /写入权限/);
  assert.doesNotMatch(page.get('heading').textContent, /请先/);
  assert.equal(page.get('tv-login-open').hidden, true);
});
