// Existing offline renderer assertions execute the actual feature bodies.
// No copied financial implementation and no network/bootstrap are evaluated.
const fs = require('node:fs');
function strip(source) {return source.replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '');}
function sourceFor(file) {
  if (!['holdings.js', 'holdings_categories.js'].includes(file)) return fs.readFileSync('frontend/public/assets/js/' + file, 'utf8');
  const kind = file === 'holdings.js' ? 'list' : 'summary';
  const base = 'frontend/public/assets/js/features/investment/holdings/';
  const view = strip(fs.readFileSync(base + kind + '_view.js', 'utf8'));
  const start = kind === 'list' ? view.indexOf('  const rowListeners') : view.indexOf('  let mobileHeroContext');
  const body = view.slice(start, view.lastIndexOf('\nreturn { '));
  const binding = kind === 'summary' ? fs.readFileSync(base + 'summary_controller.js', 'utf8').split("  scope.listen(window, 'pams:holding-group-change'", 2)[1].split('  async function load()', 1)[0] : '';
  return `const scope = {listen(target, type, callback) {target.addEventListener(type, callback);return () => {};}}; const root = undefined;\n`
    + (kind === 'summary' ? strip(fs.readFileSync(base + 'constants.js', 'utf8')) : '')
    + strip(fs.readFileSync(base + kind + '_legacy_model.js', 'utf8')) + '\n' + body
    + (binding ? "\nscope.listen(window, 'pams:holding-group-change'" + binding : '');
}
module.exports = {sourceFor};
