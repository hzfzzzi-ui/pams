import assert from 'node:assert/strict';
import {readSummaryInputs, readListInputs} from '../../frontend/public/assets/js/features/investment/holdings/requests.js';

const abort = () => Object.assign(new Error('Synthetic caller left'), {name: 'AbortError'});
const closed = path => `${path}&synthetic_closed=true`;
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};
for (const kind of ['summary', 'list']) {
  const reads = [], last = deferred();
  const summary = kind === 'summary';
  const finalPath = summary ? '/api/portfolio/valuation-state?baseCurrency=CNY' : '/api/ibkr/performance';
  const run = get => summary
    ? readSummaryInputs({get, withClosedParam: closed, loadInvestmentSummaryFresh: () => {reads.push({path: '/summary2/latest?baseCurrency=CNY', summary: true}); return Promise.resolve({synthetic: 'fresh summary'});}})
    : readListInputs({get, withClosedParam: closed});
  const loading = run((path, options) => {
    reads.push({path, options});
    return path === finalPath ? last.promise : Promise.resolve({synthetic: path});
  });
  assert.equal(reads.length, summary ? 14 : 4);
  if (summary) {
    assert.equal(reads[0].path, '/holdings/by-category/A股?minor_category=A股&limit=500&include_closed=true');
    assert.equal(reads[12].summary, true);
    assert.ok(reads.filter(read => !read.summary).every(read => read.options.cache === false));
  } else assert.equal(reads[0].path, closed('/holdings/latest'));
  // Required responses are already successful: cancelling only the last optional
  // read must still reject, rather than return an apparently successful null.
  const cancelled = assert.rejects(loading, error => error.name === 'AbortError');
  last.reject(abort()); await cancelled;

  reads.length = 0;
  const ordinary = await run(path => path === finalPath ? Promise.reject(new Error('ordinary unavailable')) : Promise.resolve({synthetic: path}));
  assert.equal(ordinary.at(-1), null, 'ordinary optional failures retain the old null fallback');
  assert.match(ordinary[0].synthetic, /^\/holdings\//);
  await assert.rejects(run(path => path.startsWith('/holdings/') ? Promise.reject(new Error('required unavailable')) : Promise.resolve({})), /required unavailable/);
}
console.log('Investment reads: fourteen/four dependency shape, fresh summary position, optional cancellation and ordinary/required failure semantics passed.');
