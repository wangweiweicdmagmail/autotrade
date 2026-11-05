/* Read-only documentation viewer; no trading state or external dependencies. */
'use strict';
const docNames = new Set(['opening-breakout', 'terminology', 'pine-contract', 'architecture', 'pine-prompt', 'night-test']);
const docLinks = {
  'OPENING_BREAKOUT_RULES.md': 'opening-breakout',
  'TERMINOLOGY.md': 'terminology',
  'PINE_INDICATOR_CONTRACT.md': 'pine-contract',
  'PINE_INDICATOR_DEVELOPMENT_PROMPT.md': 'pine-prompt',
  'ARCHITECTURE.md': 'architecture',
  'NIGHT_PAPER_TEST.md': 'night-test',
};
let pendingRequest;

function appendInline(parent, value) {
  // Build nodes rather than inserting document text as HTML.
  const tokens = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let start = 0;
  for (const match of value.matchAll(tokens)) {
    parent.append(document.createTextNode(value.slice(start, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code');
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else {
      const [, label, target] = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const file = target.split('#')[0].split('/').pop();
      const localDoc = docLinks[file];
      if (localDoc || /^https?:\/\//i.test(target)) {
        const link = document.createElement('a');
        link.textContent = label;
        link.href = localDoc ? `#${localDoc}` : target;
        if (!localDoc) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
        parent.append(link);
      } else {
        parent.append(document.createTextNode(label));
      }
    }
    start = match.index + token.length;
  }
  parent.append(document.createTextNode(value.slice(start)));
}

function renderDocument(markdown) {
  const result = document.createDocumentFragment();
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const isTable = line => /^\s*\|/.test(line);
  const cells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      const body = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      code.textContent = body.join('\n');
      pre.append(code); result.append(pre); i++; continue;
    }
    if (isTable(line) && i + 1 < lines.length && /^\s*\|[\s:|\-]+\|\s*$/.test(lines[i + 1])) {
      const wrapper = document.createElement('div'); wrapper.className = 'table-scroll';
      const table = document.createElement('table');
      const head = document.createElement('thead'); const row = document.createElement('tr');
      for (const cell of cells(line)) {
        const th = document.createElement('th'); th.scope = 'col'; appendInline(th, cell); row.append(th);
      }
      head.append(row); table.append(head); i += 2;
      const body = document.createElement('tbody');
      while (i < lines.length && isTable(lines[i])) {
        const tr = document.createElement('tr');
        for (const cell of cells(lines[i++])) { const td = document.createElement('td'); appendInline(td, cell); tr.append(td); }
        body.append(tr);
      }
      table.append(body); wrapper.append(table); result.append(wrapper); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const h = document.createElement(`h${heading[1].length}`); appendInline(h, heading[2]); result.append(h); i++; continue;
    }
    const listItem = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
    if (listItem) {
      const ordered = /^\s*\d+\./.test(line);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      while (i < lines.length) {
        const item = lines[i].match(ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*]\s+(.+)$/);
        if (!item) break;
        const li = document.createElement('li'); appendInline(li, item[1]); list.append(li); i++;
      }
      result.append(list); continue;
    }
    const paragraph = document.createElement(line.startsWith('>') ? 'blockquote' : 'p');
    appendInline(paragraph, line.replace(/^>\s?/, '')); result.append(paragraph); i++;
  }
  return result;
}

async function showDocument() {
  const requested = location.hash.slice(1);
  const name = docNames.has(requested) ? requested : 'overview';
  if (pendingRequest) pendingRequest.abort();
  for (const button of document.querySelectorAll('[data-doc]')) {
    button.setAttribute('aria-pressed', String(button.dataset.doc === name));
  }
  document.getElementById('overview').hidden = name !== 'overview';
  document.getElementById('document').hidden = name === 'overview';
  if (name === 'overview') return;
  const content = document.getElementById('doc-content');
  const status = document.getElementById('doc-status');
  const retry = document.getElementById('retry');
  const source = document.getElementById('source-link');
  source.href = `/system-docs/${name}`;
  content.replaceChildren(); retry.hidden = true;
  status.className = ''; status.textContent = '正在加载项目文档…';
  const request = new AbortController(); pendingRequest = request;
  try {
    const response = await fetch(source.href, { signal: request.signal, cache: 'no-store' });
    if (!response.ok) throw new Error('加载失败');
    const markdown = await response.text();
    if (request.signal.aborted) return;
    content.replaceChildren(renderDocument(markdown)); status.textContent = '';
  } catch (error) {
    if (request.signal.aborted) return;
    status.className = 'error'; status.textContent = '文档加载失败，请重试，或点击上方链接查看原文。'; retry.hidden = false;
  }
}

for (const button of document.querySelectorAll('[data-doc]')) {
  button.addEventListener('click', () => {
    if (location.hash === `#${button.dataset.doc}`) showDocument();
    else location.hash = button.dataset.doc;
  });
}
document.getElementById('retry').addEventListener('click', showDocument);
window.addEventListener('hashchange', showDocument);
const zoom = document.getElementById('zoom');
const image = document.getElementById('architecture-image');
function setZoom(value) {
  zoom.value = value;
  image.style.width = `${value}%`;
  document.getElementById('zoom-value').textContent = Number(value) === 100 ? '适应宽度' : `${value}%`;
}
zoom.addEventListener('input', () => setZoom(zoom.value));
document.getElementById('fit').addEventListener('click', () => {
  setZoom(100); document.querySelector('.diagram-viewport').scrollTo(0, 0);
});
image.addEventListener('error', () => { document.getElementById('image-error').hidden = false; });
image.addEventListener('load', () => { document.getElementById('image-error').hidden = true; });
showDocument();
