import test from 'node:test';
import assert from 'node:assert/strict';
import {CURRENCIES, MAX_QUOTE_AGE_MS, createCurrencyController, parseFxQuote,
  formatQuoteAmount, formatQuoteRatio} from './dist/currency.mjs';

const NOW = Date.parse('2026-11-24T03:30:00Z');
function quote(symbol = 'EURUSD', at = NOW, values = {}) {
  return {symbol, bid: '1.24', ask: '1.26', mid: '1.25', timestamp: new Date(at).toISOString(),
    lastQuoteAt: new Date(at).toISOString(), stale: false, marketState: 'open', quoteAgeSeconds: '0', ...values};
}
const response = value => ({ok: true, status: 200, text: async () => JSON.stringify({[value.symbol]: value})});
const usd = () => createCurrencyController().getSnapshot();
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
function clock() {
  let now = NOW, next = 0;
  const pending = new Map();
  return {now: () => now, pending,
    setTimer(fn, delay) { const id = ++next; pending.set(id, {fn, at: now + delay}); return id; },
    clearTimer(id) { pending.delete(id); },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const [id, task] = [...pending].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0] || [];
        if (!task) break;
        now = task.at; pending.delete(id); task.fn();
      }
      now = end;
    },
  };
}
const controller = (timer, options = {}) => createCurrencyController({initialCurrency: 'EUR',
  now: timer.now, setTimer: timer.setTimer, clearTimer: timer.clearTimer, ...options});

test('major currencies are allowlisted and USD is immediate without any FX request', async () => {
  assert.equal(CURRENCIES.length, 15);
  let calls = 0;
  const c = createCurrencyController({fetcher: async () => { calls++; throw new Error(); }});
  await c.start();
  assert.equal(c.getSnapshot().status, 'ready');
  assert.equal(formatQuoteAmount('2850979415123', c.getSnapshot()), '$2,850,979.42');
  assert.equal(calls, 0);
  assert.throws(() => c.select('BTC'));
  c.stop();
});

test('inverse EUR/GBP/AUD rates and direct USD pairs use exact rational arithmetic', () => {
  for (const code of ['EUR', 'GBP', 'AUD']) {
    const state = parseFxQuote(quote(`${code}USD`), code, NOW);
    assert.deepEqual(state.rate, {numerator: 4n, denominator: 5n});
  }
  const cad = parseFxQuote(quote('USDCAD'), 'CAD', NOW);
  assert.deepEqual(cad.rate, {numerator: 5n, denominator: 4n});
  assert.equal(formatQuoteAmount(1_000_000n, cad, {now: NOW}), 'CA$1.25');
  assert.equal(formatQuoteAmount(1_000_000n, parseFxQuote(quote(), 'EUR', NOW), {now: NOW}), '€0.80');
});

test('large monetary values never pass through floating point and rounding is exact', () => {
  assert.equal(formatQuoteAmount('9007199254740993123456789', usd()), '$9,007,199,254,740,993,123.46');
  assert.equal(formatQuoteAmount('1004999', usd()), '$1.00');
  assert.equal(formatQuoteAmount('1005000', usd()), '$1.01');
  assert.equal(formatQuoteRatio(10101886n * 1_000_000_000n, 709700000000n, usd(), {maximumFractionDigits: 8}), '$0.01423402');
});

test('compact output and currency minor units remain precise and unambiguous', () => {
  assert.equal(formatQuoteAmount('2850979415123', usd(), {compact: true}), '$2.85M');
  assert.equal(formatQuoteAmount('999999999999', usd(), {compact: true}), '$1.00M');
  const jpy = parseFxQuote(quote('USDJPY', NOW, {bid: '149.5', ask: '150.5', mid: '150'}), 'JPY', NOW);
  assert.equal(formatQuoteAmount('1000000', jpy, {now: NOW}), '¥150 JPY');
  assert.equal(formatQuoteAmount('1000000', usd(), {locale: 'de-DE'}), '1,00 $');
});

test('unsafe amounts, zero denominators, malformed rates and inconsistent midpoint fail', () => {
  for (const value of [9007199254740992, '1.25', '-1', '1e6', true, null, '9'.repeat(79)]) {
    assert.throws(() => formatQuoteAmount(value, usd()));
  }
  assert.throws(() => formatQuoteRatio(1n, 0n, usd()));
  for (const change of [{mid: 'NaN'}, {mid: 'Infinity'}, {mid: '-1'}, {mid: '0'},
    {mid: '1.251'}, {bid: '1.30'}, {ask: '1e999'}, {mid: 1.25}, {symbol: 'GBPUSD'}]) {
    assert.throws(() => parseFxQuote(quote('EURUSD', NOW, change), 'EUR', NOW));
  }
});

test('stale, closed, old, future and inconsistent quote timestamps cannot become live', () => {
  for (const change of [{stale: true}, {marketState: 'closed'}, {quoteAgeSeconds: '60'},
    {timestamp: new Date(NOW - 60001).toISOString()}, {lastQuoteAt: new Date(NOW - 1).toISOString()},
    {timestamp: new Date(NOW + 5001).toISOString(), lastQuoteAt: new Date(NOW + 5001).toISOString()},
    {timestamp: 'yesterday'}, {quoteAgeSeconds: '-1'}]) {
    assert.throws(() => parseFxQuote(quote('EURUSD', NOW, change), 'EUR', NOW));
  }
  const state = parseFxQuote(quote(), 'EUR', NOW);
  assert.equal(state.asOf, NOW);
  assert.equal(formatQuoteAmount('1000000', state, {now: NOW + MAX_QUOTE_AGE_MS}), null);
});

test('selected-currency fetch is unauthenticated, strict and browser-cache bypassed', async () => {
  const timer = clock(), calls = [];
  const c = controller(timer, {fetcher: async (url, options) => { calls.push({url, options}); return response(quote()); }});
  await c.start();
  assert.equal(calls[0].url, 'https://biquote.io/api/latest?symbols=EURUSD&allowStale=false');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.equal(calls[0].options.credentials, 'omit');
  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(c.getSnapshot().source, 'biquote');
  assert.equal(c.getSnapshot().asOf, NOW);
  c.stop();
  assert.equal(timer.pending.size, 0);
});

test('out-of-order selected currency responses cannot replace the new selection', async () => {
  const timer = clock(), eur = deferred(), jpy = deferred();
  const c = controller(timer, {fetcher: url => url.includes('EURUSD') ? eur.promise : jpy.promise});
  const first = c.start(); await flush();
  const second = c.select('JPY'); await flush();
  jpy.resolve(response(quote('USDJPY', NOW, {bid: '149', ask: '151', mid: '150'})));
  await second;
  eur.resolve(response(quote())); await first; await flush();
  assert.equal(c.getSnapshot().currency, 'JPY');
  assert.equal(c.getSnapshot().rate.numerator, 150n);
  c.stop();
});

test('timeout completes even if fetch ignores abort and late success remains discarded', async () => {
  const timer = clock(), pending = deferred();
  let signal;
  const c = controller(timer, {fetcher: (_, options) => { signal = options.signal; return pending.promise; }});
  const task = c.start(); await flush();
  timer.advance(6000); await task;
  assert.equal(signal.aborted, true);
  assert.equal(c.getSnapshot().status, 'unavailable');
  pending.resolve(response(quote())); await flush();
  assert.equal(c.getSnapshot().rate, null);
  c.stop();
});

test('FX rejects oversized response bodies before they can become a monetary rate', async () => {
  for(const headers of [{},{'Content-Length':'1'},{'Content-Length':'65537'}]) {
    const timer=clock();
    const c=controller(timer,{fetcher:async () => new Response('x'.repeat(65537),{headers})});
    await c.start();
    assert.equal(c.getSnapshot().status,'unavailable');assert.equal(c.getSnapshot().rate,null);
    assert.equal(formatQuoteAmount('1000000',c.getSnapshot(),{now:timer.now()}),null);
    c.stop();
  }
});

test('foreground refresh retains still-fresh quote only while pending then clears on failure', async () => {
  const timer = clock(), pending = deferred();
  let calls = 0;
  const c = controller(timer, {fetcher: () => ++calls === 1 ? response(quote()) : pending.promise});
  await c.start();
  const refresh = c.refresh(); await flush();
  assert.equal(c.getSnapshot().status, 'ready');
  assert.equal(c.getSnapshot().refreshing, true);
  pending.reject(new Error('offline')); await refresh;
  assert.equal(c.getSnapshot().status, 'unavailable');
  assert.equal(formatQuoteAmount('1000000', c.getSnapshot(), {now: timer.now()}), null);
  c.stop();
});

test('expiry invalidates display independently of the next refresh or a hung request', async () => {
  const timer = clock(), pending = deferred(), updates = [];
  let calls = 0;
  const c = controller(timer, {timeoutMs: 15000, onChange: s => updates.push(s.status),
    fetcher: () => ++calls === 1 ? response(quote('EURUSD', NOW - 55000, {quoteAgeSeconds: '55'})) : pending.promise});
  await c.start();
  const task = c.refresh(); await flush();
  timer.advance(5000);
  assert.equal(c.getSnapshot().status, 'unavailable');
  assert.equal(updates.at(-1), 'unavailable');
  c.stop(); await task;
});

test('stop drops rates and cancels work; resume fetches again without stored FX', async () => {
  const timer = clock(); let calls = 0;
  const c = controller(timer, {fetcher: async () => { calls++; return response(quote()); }});
  await c.start(); c.stop();
  assert.equal(c.getSnapshot().rate, null);
  assert.equal(timer.pending.size, 0);
  await c.start();
  assert.equal(calls, 2);
  c.stop();
});

test('periodic refresh runs in foreground only and missing batch symbol is unavailable', async () => {
  const timer = clock(); let calls = 0;
  const c = controller(timer, {fetcher: async () => {
    calls++; return calls === 1 ? response(quote()) : {ok: true, text: async () => '{}'};
  }});
  await c.start(); timer.advance(12000); await flush();
  assert.equal(calls, 2);
  assert.equal(c.getSnapshot().status, 'unavailable');
  c.stop(); timer.advance(120000); await flush();
  assert.equal(calls, 2);
});

test('429 Retry-After is honored across refresh and currency selection without stale fallback', async () => {
  const timer = clock(); let calls = 0;
  const c = controller(timer, {fetcher: async url => {
    calls++;
    return calls === 1 ? {ok: false, status: 429, headers: {get: () => '120'}} : response(quote(url.includes('GBPUSD') ? 'GBPUSD' : 'EURUSD', timer.now()));
  }});
  await c.start();
  assert.equal(c.getSnapshot().status, 'unavailable');
  await c.refresh(); await c.select('GBP');
  assert.equal(calls, 1);
  timer.advance(119999); await flush(); assert.equal(calls, 1);
  timer.advance(1); await flush(); assert.equal(calls, 2);
  assert.equal(c.getSnapshot().status, 'ready');
  c.stop();
});

test('503 HTTP-date Retry-After and hidden retry headers use bounded polite retry behavior', async () => {
  for (const header of [new Date(NOW + 90000).toUTCString(), null]) {
    const timer = clock(); let calls = 0;
    const c = controller(timer, {fetcher: async () => { calls++; return {ok: false, status: 503, headers: {get: () => header}}; }});
    await c.start();
    timer.advance(header ? 89999 : 59999); await flush();
    assert.equal(calls, 1);
    timer.advance(1); await flush(); assert.equal(calls, 2);
    c.stop();
  }
});
