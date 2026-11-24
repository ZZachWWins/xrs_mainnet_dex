import {parseExactJSON, readBoundedText} from './model.mjs';

export const FX_SOURCE_URL = 'https://biquote.io/';
export const MAX_QUOTE_AGE_MS = 60_000;
export const CURRENCIES = Object.freeze([
  ['USD', 'US Dollar'], ['EUR', 'Euro'], ['GBP', 'British Pound'], ['JPY', 'Japanese Yen'],
  ['CHF', 'Swiss Franc'], ['CAD', 'Canadian Dollar'], ['AUD', 'Australian Dollar'],
  ['CNY', 'Chinese Yuan'], ['INR', 'Indian Rupee'], ['KRW', 'South Korean Won'],
  ['BRL', 'Brazilian Real'], ['SGD', 'Singapore Dollar'], ['AED', 'UAE Dirham'],
  ['ZAR', 'South African Rand'], ['MXN', 'Mexican Peso'],
].map(([code, name]) => Object.freeze({code, label: `${code} — ${name}`})));
const CODES = new Set(CURRENCIES.map(c => c.code));
const INVERTED = new Set(['EUR', 'GBP', 'AUD']);
const MAX_RAW_DIGITS = 78;

function currency(code) {
  if (!CODES.has(code)) throw new Error('Unsupported display currency.');
  return code;
}

function rawInteger(value, positive = false) {
  if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error('Exact monetary amount is unavailable.');
  if (!['string', 'bigint', 'number'].includes(typeof value) ||
      !/^(0|[1-9][0-9]*)$/.test(String(value)) || String(value).length > MAX_RAW_DIGITS) {
    throw new Error('Invalid monetary amount.');
  }
  const result = BigInt(value);
  if (positive && !result) throw new Error('Amount denominator must be positive.');
  return result;
}

function gcd(a, b) {
  while (b) [a, b] = [b, a % b];
  return a;
}

function ratio(numerator, denominator) {
  const factor = gcd(numerator, denominator);
  return Object.freeze({numerator: numerator / factor, denominator: denominator / factor});
}

function decimalRatio(value) {
  // Provider JSON numbers have been retained as their original decimal lexemes.
  if (typeof value !== 'string' || value.length > 80) throw new Error('Invalid FX rate.');
  const match = /^(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/.exec(value);
  if (!match || match[1].length > 24 || (match[2]?.length || 0) > 24) throw new Error('Invalid FX rate.');
  const exponent = Number(match[3] || 0);
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 24) throw new Error('Invalid FX exponent.');
  const scale = (match[2]?.length || 0) - exponent;
  let n = BigInt(match[1] + (match[2] || '')), d = 1n;
  if (scale >= 0) d = 10n ** BigInt(scale);
  else n *= 10n ** BigInt(-scale);
  if (!n || n > d * 10n ** 12n) throw new Error('FX rate is outside the supported range.');
  return ratio(n, d);
}

function compare(a, b) {
  const delta = a.numerator * b.denominator - b.numerator * a.denominator;
  return delta < 0n ? -1 : delta > 0n ? 1 : 0;
}

function utcTimestamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z$/.test(value)) {
    throw new Error('FX quote timestamp is unavailable.');
  }
  const result = Date.parse(value);
  if (!Number.isFinite(result)) throw new Error('Invalid FX quote timestamp.');
  return result;
}

export function parseFxQuote(data, code, now = Date.now()) {
  currency(code);
  if (code === 'USD') throw new Error('USD does not require an FX quote.');
  const symbol = INVERTED.has(code) ? `${code}USD` : `USD${code}`;
  if (data?.symbol !== symbol || data?.stale !== false || data?.marketState !== 'open') {
    throw new Error('Live FX is unavailable for this currency.');
  }
  const asOf = utcTimestamp(data.timestamp), lastQuote = utcTimestamp(data.lastQuoteAt);
  const reportedAge = rawInteger(data.quoteAgeSeconds);
  if (!Number.isFinite(now) || asOf !== lastQuote || asOf > now + 5000 ||
      now - asOf >= MAX_QUOTE_AGE_MS || reportedAge >= 60n) {
    throw new Error('FX quote is stale or its timestamp is inconsistent.');
  }
  const bid = decimalRatio(data.bid), ask = decimalRatio(data.ask), mid = decimalRatio(data.mid);
  if (compare(bid, ask) > 0 || compare(mid, bid) < 0 || compare(mid, ask) > 0) {
    throw new Error('FX quote prices are inconsistent.');
  }
  const midpoint = ratio(bid.numerator * ask.denominator + ask.numerator * bid.denominator,
                         2n * bid.denominator * ask.denominator);
  if (compare(mid, midpoint) !== 0) throw new Error('FX midpoint is inconsistent.');
  return Object.freeze({currency: code, status: 'ready',
    rate: INVERTED.has(code) ? ratio(mid.denominator, mid.numerator) : mid,
    asOf, fetchedAt: now, expiresAt: asOf + MAX_QUOTE_AGE_MS,
    source: 'biquote', sourceUrl: FX_SOURCE_URL, reason: null});
}

function emptySnapshot(code, status, reason = null) {
  return Object.freeze({currency: code, status, rate: null, asOf: null, fetchedAt: null,
    expiresAt: null, source: code === 'USD' ? 'USD denomination' : 'biquote',
    sourceUrl: code === 'USD' ? null : FX_SOURCE_URL, reason});
}

function usdSnapshot() {
  return Object.freeze({...emptySnapshot('USD', 'ready'), rate: ratio(1n, 1n)});
}

function retryDelay(response, now) {
  const header = response.headers?.get?.('Retry-After');
  if (typeof header === 'string' && /^\d+$/.test(header.trim())) {
    const milliseconds = Number(header.trim()) * 1000;
    if (Number.isSafeInteger(milliseconds)) return milliseconds;
  }
  if (typeof header === 'string') {
    const date = Date.parse(header);
    if (Number.isFinite(date)) return Math.max(0, date - now);
  }
  return 60_000;
}

export function createCurrencyController({onChange = () => {}, fetcher = globalThis.fetch,
  now = Date.now, intervalMs = 12_000, timeoutMs = 6000, initialCurrency = 'USD',
  setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout} = {}) {
  currency(initialCurrency);
  if (!Number.isFinite(intervalMs) || intervalMs < 10_000 || intervalMs > 15_000 ||
      !Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 15_000) throw new Error('Invalid FX refresh timing.');
  let state = initialCurrency === 'USD' ? usdSnapshot() : emptySnapshot(initialCurrency, 'unavailable', 'Live FX has not been requested.');
  let active = false, generation = 0, aborter = null, refreshTimer = null, expiryTimer = null;
  let cancelPending = null, retryUntil = 0;
  const publish = value => { state = value; onChange(value); };
  function cancel() {
    generation++;
    cancelPending?.(); cancelPending = null;
    aborter?.abort(); aborter = null;
    if (refreshTimer !== null) clearTimer(refreshTimer);
    if (expiryTimer !== null) clearTimer(expiryTimer);
    refreshTimer = expiryTimer = null;
  }
  function expire() {
    if (state.currency !== 'USD' && state.status === 'ready' && state.expiresAt <= now()) {
      publish(emptySnapshot(state.currency, 'unavailable', 'Live FX quote expired.'));
    }
  }
  function getSnapshot() { expire(); return state; }
  async function refresh() {
    if (!active) return getSnapshot();
    const code = state.currency;
    cancel();
    if (code === 'USD') { publish(usdSnapshot()); return state; }
    if (retryUntil > now()) {
      publish(emptySnapshot(code, 'unavailable', 'FX provider requested a retry delay.'));
      refreshTimer = setTimer(() => { void refresh(); }, Math.min(2_147_483_647, retryUntil - now()));
      return state;
    }
    const requestGeneration = generation;
    const controller = new AbortController(); aborter = controller;
    if (state.status === 'ready' && state.expiresAt > now()) {
      publish(Object.freeze({...state, refreshing: true}));
      expiryTimer = setTimer(expire, Math.max(0, state.expiresAt - now()));
    } else publish(emptySnapshot(code, 'loading'));
    let timeout;
    const timeoutPromise = new Promise((_, reject) => {
      timeout = setTimer(() => { controller.abort(); reject(new Error('Live FX request timed out.')); }, timeoutMs);
      cancelPending = () => { clearTimer(timeout); reject(new Error('FX request cancelled.')); };
    });
    const request = Promise.resolve().then(async () => {
      const symbol = INVERTED.has(code) ? `${code}USD` : `USD${code}`;
      const response = await fetcher(`${FX_SOURCE_URL}api/latest?symbols=${symbol}&allowStale=false`, {
        method: 'GET', mode: 'cors', credentials: 'omit', redirect: 'error', cache: 'no-store',
        headers: {Accept: 'application/json'}, signal: controller.signal,
      });
      if (!response.ok) {
        const error = new Error(response.status === 404 ? 'Live FX is unavailable or the market is closed.' : 'Live FX request failed.');
        if (response.status === 429 || response.status === 503) error.retryAfterMs = retryDelay(response, now());
        throw error;
      }
      const text = await readBoundedText(response, {maximumBytes:65_536, signal:controller.signal});
      return parseFxQuote(parseExactJSON(text)?.[symbol], code, now());
    });
    try {
      const next = await Promise.race([request, timeoutPromise]);
      if (active && requestGeneration === generation && state.currency === code) {
        publish(next);
        if (expiryTimer !== null) clearTimer(expiryTimer);
        expiryTimer = setTimer(expire, Math.max(0, next.expiresAt - now()));
      }
    } catch (error) {
      if (active && requestGeneration === generation && state.currency === code) {
        if (Number.isFinite(error?.retryAfterMs)) retryUntil = Math.max(retryUntil, now() + error.retryAfterMs);
        if (expiryTimer !== null) clearTimer(expiryTimer);
        expiryTimer = null;
        // Drop old rates on every failure; never fall back to a previous quote.
        publish(emptySnapshot(code, 'unavailable', error?.message || 'Live FX is unavailable.'));
      }
    } finally {
      clearTimer(timeout);
      if (active && requestGeneration === generation) {
        aborter = null; cancelPending = null;
        refreshTimer = setTimer(() => { void refresh(); }, Math.min(2_147_483_647, Math.max(intervalMs, retryUntil - now())));
      }
    }
    return getSnapshot();
  }
  return Object.freeze({getSnapshot, refresh,
    start() { if (active) return Promise.resolve(getSnapshot()); active = true; return refresh(); },
    stop() {
      active = false; cancel();
      if (state.currency !== 'USD') publish(emptySnapshot(state.currency, 'unavailable', 'Live FX is paused.'));
    },
    select(code) {
      currency(code); cancel();
      publish(code === 'USD' ? usdSnapshot() : emptySnapshot(code, active ? 'loading' : 'unavailable', active ? null : 'Live FX is paused.'));
      return active ? refresh() : Promise.resolve(state);
    },
  });
}

function displayRate(snapshot, now) {
  if (!snapshot || !CODES.has(snapshot.currency) || snapshot.status !== 'ready') return null;
  if (snapshot.currency === 'USD') return ratio(1n, 1n);
  if (!Number.isFinite(snapshot.asOf) || !Number.isFinite(snapshot.expiresAt) ||
      snapshot.expiresAt <= now || snapshot.expiresAt !== snapshot.asOf + MAX_QUOTE_AGE_MS ||
      snapshot.asOf > now + 5000 || !snapshot.rate) return null;
  return ratio(rawInteger(snapshot.rate.numerator, true), rawInteger(snapshot.rate.denominator, true));
}

export function formatQuoteAmount(rawXUSDC, snapshot, options = {}) {
  return formatQuoteRatio(rawXUSDC, 1n, snapshot, options);
}

export function formatQuoteRatio(numeratorRaw, denominator, snapshot, {
  locale = 'en-US', maximumFractionDigits, minimumFractionDigits, compact = false, now = Date.now(),
} = {}) {
  const input = rawInteger(numeratorRaw), divisor = rawInteger(denominator, true);
  const rate = displayRate(snapshot, now);
  if (!rate) return null;
  const code = currency(snapshot.currency);
  const defaults = new Intl.NumberFormat(locale, {style: 'currency', currency: code}).resolvedOptions();
  const digits = maximumFractionDigits ?? defaults.maximumFractionDigits;
  const minimum = minimumFractionDigits ?? Math.min(defaults.minimumFractionDigits, digits);
  if (!Number.isInteger(digits) || digits < 0 || digits > 18 || !Number.isInteger(minimum) || minimum < 0 || minimum > digits) {
    throw new Error('Invalid monetary display precision.');
  }
  const n = input * rate.numerator, d = divisor * rate.denominator * 1_000_000n;
  const powers = [1n, 1000n, 1_000_000n, 1_000_000_000n, 1_000_000_000_000n];
  const suffixes = ['', 'K', 'M', 'B', 'T'];
  let index = 0;
  if (compact) while (index < powers.length - 1 && n >= d * powers[index + 1]) index++;
  const precision = 10n ** BigInt(digits);
  const rounded = scale => (2n * n * precision + d * scale) / (2n * d * scale);
  let units = rounded(powers[index]);
  if (compact && index < powers.length - 1 && units >= 1000n * precision) {
    index++; units = rounded(powers[index]);
  }
  const whole = units / precision;
  let fraction = digits ? (units % precision).toString().padStart(digits, '0') : '';
  while (fraction.length > minimum && fraction.endsWith('0')) fraction = fraction.slice(0, -1);
  const template = new Intl.NumberFormat(locale, {style: 'currency', currency: code,
    currencyDisplay: 'symbol', minimumFractionDigits: Math.max(1, minimum), maximumFractionDigits: Math.max(1, digits)}).formatToParts(0n);
  const first = template.findIndex(p => p.type === 'integer');
  const last = template.findLastIndex(p => ['integer', 'group', 'decimal', 'fraction'].includes(p.type));
  const decimal = template.find(p => p.type === 'decimal')?.value || '.';
  const number = new Intl.NumberFormat(locale, {maximumFractionDigits: 0}).format(whole) + (fraction ? decimal + fraction : '');
  let result = template.slice(0, first).map(p => p.value).join('') + number + suffixes[index] + template.slice(last + 1).map(p => p.value).join('');
  const symbol = template.find(p => p.type === 'currency')?.value;
  if (code !== 'USD' && (symbol === '$' || symbol === '¥' || symbol === '￥')) result += ` ${code}`;
  return result;
}
