#!/usr/bin/env node
// Backup price capture for the carbon-market brief.
// Usage:
//   node scripts/capture.cjs                     -> writes market-captures/<KST date>/capture_<HHMM>_kst.json
//   node scripts/capture.cjs --add-investing F   -> parses a markdown/text table in file F (Investing rows
//                                                   obtained with WebFetch) into the latest capture file
// Never guesses: anything that cannot be read with a date is recorded as FAILED with a reason.
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const URLS = {
  EUA: 'https://www.ice.com/products/197/EUA-Futures/data?marketId=8997105',
  UKA: 'https://www.ice.com/products/80216150/UKA-Futures/data?marketId=7599535',
  NZU: 'https://www.emstradepoint.co.nz/',
  ACCU: 'https://coremarkets.co/resources/market-prices',
  INV: 'https://www.investing.com/commodities/carbon-emissions-historical-data',
};
const MON = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };

const sha = (s) => crypto.createHash('sha256').update(s, 'utf8').digest('hex');
const pad = (n) => String(n).padStart(2, '0');
const num = (s) => (s == null ? null : Number(String(s).replace(/[,$A%+]/g, '').replace(/^▼\s*/, '-').replace(/^▲\s*/, '')));
const failed = (url, basis, why) => ({ status: 'FAILED', url, source_text: why, trade_date: null, value: null, percent_change: null, basis });

function kstNow() {
  const d = new Date(Date.now() + 9 * 3600e3);
  return { date: `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`, hhmm: pad(d.getUTCHours()) + pad(d.getUTCMinutes()) };
}

// Plain fetch through the environment proxy, then strip tags to visible text lines.
function curlText(url) {
  const html = execFileSync('curl', ['-sSL', '--fail', '-m', '40', '-A', 'Mozilla/5.0', url], { encoding: 'utf8', maxBuffer: 20e6 });
  return html
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, '')
    .replace(/<[^>]+>/g, '\n')
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&nbsp;/g, ' ').replace(/&ndash;/g, '–').replace(/&amp;/g, '&')
    .split('\n').map((l) => l.trim()).filter(Boolean);
}

function parseNZU(lines) {
  const i = lines.findIndex((l) => /CP-NZU/.test(l));
  if (i < 0) return failed(URLS.NZU, 'DAILY_INDEX', 'CP-NZU block not found');
  const blk = lines.slice(i, i + 12);
  const dm = blk[1] && blk[1].match(/^(\d{1,2}) (\w{3}) (\d{4})$/);
  const ei = blk.findIndex((l) => /^ECMI/.test(l));
  if (!dm || ei < 0) return failed(URLS.NZU, 'DAILY_INDEX', 'no dated ECMI: ' + blk.join(' | ').slice(0, 300));
  const value = num(blk[ei + 2]);
  const dir = blk[ei + 4]; const pct = num(blk[ei + 5]);
  const out = {
    status: 'OK', url: URLS.NZU, source_text: blk.join(' | ').slice(0, 600),
    trade_date: `${dm[3]}-${pad(MON[dm[2].toLowerCase()])}-${pad(dm[1])}`, value,
    percent_change: dir === '▼' ? -pct : pct, basis: 'DAILY_INDEX',
  };
  const qi = lines.findIndex((l) => /^ECQI/.test(l));
  if (qi > 0) out.ecqi = num(lines[qi + 2]);
  const ti = lines.findIndex((l, k) => l === 'Last trade' && /\$\/NZU/.test(lines[k + 1] || ''));
  if (ti > 0) out.last_trade_no_date = num(lines[ti + 2]);
  return out;
}

function parseACCU(lines) {
  const i = lines.findIndex((l) => /Prices last updated end of day/.test(l));
  if (i < 0) return failed(URLS.ACCU, 'SPOT_CLOSE', 'update-date line not found');
  const blk = lines.slice(i, i + 4);
  const dm = blk[0].match(/(\d{2})\/(\d{2})\/(\d{4})/);
  const vm = blk.join(' ').match(/Last close A\$([\d.,]+)\s*([▲▼])?\s*([\d.]+)%/);
  if (!dm || !vm) return failed(URLS.ACCU, 'SPOT_CLOSE', 'unparsed: ' + blk.join(' | '));
  return {
    status: 'OK', url: URLS.ACCU, source_text: blk.join(' | '), trade_date: `${dm[3]}-${dm[2]}-${dm[1]}`,
    value: num(vm[1]), percent_change: (vm[2] === '▼' ? -1 : 1) * Number(vm[3]), basis: 'SPOT_CLOSE',
  };
}

// ICE pages render the price table with JavaScript, so a real browser is needed.
// Requires static.ice.com to be allowed by the environment network policy.
async function iceRows(browser, url) {
  const page = await browser.newPage();
  const blocked = new Set();
  page.on('requestfailed', (r) => blocked.add(new URL(r.url()).host));
  try {
    await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForSelector('table tr', { timeout: 20000 }).catch(() => {});
    return { rows: await page.$$eval('table tr', (trs) => trs.map((tr) => [...tr.children].map((c) => c.innerText.trim()))), blocked };
  } finally { await page.close(); }
}

function parseICE(name, res) {
  const url = URLS[name];
  const hi = res.rows.findIndex((r) => r.some((c) => /^last$/i.test(c)));
  const row = res.rows.find((r) => /^dec ?26$/i.test((r[0] || '').replace(/\s+/g, ' ')));
  if (hi < 0 || !row) {
    const why = res.blocked.size ? `price table not rendered; blocked hosts: ${[...res.blocked].join(', ')}` : 'DEC26 row not found';
    return failed(url, 'FUTURES_LAST_TRADE', why);
  }
  const head = res.rows[hi];
  const col = (re) => { const k = head.findIndex((h) => re.test(h)); return k < 0 ? null : row[k]; };
  const time = col(/time/i) || '';
  const dm = time.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  const src = head.join(' | ') + '\n' + row.join(' | ');
  if (!dm) return { ...failed(url, 'FUTURES_LAST_TRADE', 'no date in row: ' + src.slice(0, 500)) };
  return {
    status: 'OK', url, source_text: src.slice(0, 600), trade_date: `${dm[3]}-${pad(dm[1])}-${pad(dm[2])}`,
    time_gmt: time, value: num(col(/^last$/i)), percent_change: num(col(/%/)), volume: num(col(/vol/i)),
    basis: 'FUTURES_LAST_TRADE',
  };
}

// Investing rows from any text containing lines like "Nov 06, 2026 | 86.55 | 89.12 | 89.13 | 86.25 | 31.31K | -2.70%".
function parseInvestingText(text) {
  const rows = [];
  for (const l of text.split('\n')) {
    const m = l.match(/(\w{3}) (\d{2}), (\d{4})\D+?([\d.,]+)\D+?([\d.,]+)\D+?([\d.,]+)\D+?([\d.,]+)\s*\|?\s*([\d.,]+[KM]?|-)\s*\|?\s*([+-]?[\d.]+%)/);
    if (m && MON[m[1].toLowerCase()]) rows.push({
      trade_date: `${m[3]}-${pad(MON[m[1].toLowerCase()])}-${m[2]}`, price: m[4], open: m[5], high: m[6], low: m[7],
      vol: m[8], change_pct: m[9], basis: 'FUTURES_SETTLEMENT', _line: l.trim(),
    });
  }
  return rows.slice(0, 10);
}

function investingEntry(rows, note) {
  if (!rows.length) return { inv: failed(URLS.INV, 'FUTURES_SETTLEMENT', note || 'no rows'), rows: [] };
  const src = rows.map((r) => r._line).join('\n').slice(0, 600);
  rows.forEach((r) => delete r._line);
  return {
    inv: { status: 'OK', url: URLS.INV, source_text: src, trade_date: rows[0].trade_date, value: num(rows[0].price),
      percent_change: num(rows[0].change_pct), basis: 'FUTURES_SETTLEMENT', note },
    rows,
  };
}

function finalize(doc) {
  for (const m of Object.values(doc.markets)) m.sha256 = sha(m.source_text || '');
  return doc;
}

function summary(doc) {
  return Object.entries(doc.markets).map(([k, m]) => `${k}: ${m.status} ${m.trade_date || '-'} ${m.value ?? '-'}${m.status === 'FAILED' ? ' (' + m.source_text.slice(0, 80) + ')' : ''}`).join('\n');
}

async function main() {
  const args = process.argv.slice(2);
  if (args[0] === '--add-investing') {
    const { date } = kstNow();
    const dir = path.join(ROOT, 'market-captures', date);
    const file = fs.readdirSync(dir).filter((f) => /^capture_\d{4}_kst\.json$/.test(f)).sort().pop();
    const doc = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));
    const { inv, rows } = investingEntry(parseInvestingText(fs.readFileSync(args[1], 'utf8')), 'curl 403; rows read via WebFetch');
    doc.markets.EUA_INVESTING = inv; doc.investing_eua_rows = rows;
    fs.writeFileSync(path.join(dir, file), JSON.stringify(finalize(doc), null, 1) + '\n');
    console.log(path.relative(ROOT, path.join(dir, file)) + '\n' + summary(doc));
    return;
  }

  const { date, hhmm } = kstNow();
  const doc = { captured_at_utc: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), markets: {}, investing_eua_rows: [] };

  for (const [k, parse] of [['NZU', parseNZU], ['ACCU', parseACCU]]) {
    try { doc.markets[k] = parse(curlText(URLS[k])); } catch (e) { doc.markets[k] = failed(URLS[k], k === 'NZU' ? 'DAILY_INDEX' : 'SPOT_CLOSE', 'fetch error: ' + e.message.slice(0, 200)); }
  }

  let browser;
  try {
    browser = await require('playwright').chromium.launch();
    for (const k of ['EUA', 'UKA']) {
      try { doc.markets[k] = parseICE(k, await iceRows(browser, URLS[k])); } catch (e) { doc.markets[k] = failed(URLS[k], 'FUTURES_LAST_TRADE', 'browser error: ' + e.message.slice(0, 200)); }
    }
  } catch (e) {
    for (const k of ['EUA', 'UKA']) doc.markets[k] = failed(URLS[k], 'FUTURES_LAST_TRADE', 'browser launch failed: ' + e.message.slice(0, 200));
  } finally { if (browser) await browser.close(); }

  try {
    const r = investingEntry(parseInvestingText(curlText(URLS.INV).join(' | ').replace(/ \| (?=\w{3} \d{2}, \d{4})/g, '\n')), 'curl');
    doc.markets.EUA_INVESTING = r.inv; doc.investing_eua_rows = r.rows;
  } catch (e) {
    doc.markets.EUA_INVESTING = failed(URLS.INV, 'FUTURES_SETTLEMENT', 'NEEDS_WEBFETCH: ' + e.message.slice(0, 120));
  }

  const dir = path.join(ROOT, 'market-captures', date);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `capture_${hhmm}_kst.json`);
  fs.writeFileSync(file, JSON.stringify(finalize(doc), null, 1) + '\n');
  console.log(path.relative(ROOT, file) + '\n' + summary(doc));
}

main().catch((e) => { console.error(e); process.exit(1); });
