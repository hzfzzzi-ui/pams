import {readFileSync} from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

// Use real feature modules and presentation renderers; only the network, shell
// bootstrap and unrelated NAV renderer are injectable at this boundary.
export async function loadFeatureModule(publicRoot, entry, globals = {}, overrides = {}) {
  const context = vm.createContext({URLSearchParams, ...globals});
  const cache = new Map();
  const jsRoot = path.join(publicRoot, 'assets/js');
  const resolve = (file, specifier) => path.resolve(path.dirname(file), specifier.split('?')[0]);
  function getModule(file) {
    if (cache.has(file)) return cache.get(file);
    let module;
    if (file === path.join(jsRoot, 'api.js')) {
      const values = {jget: overrides.get || (() => Promise.resolve({})), jpost: overrides.post || (() => Promise.resolve({}))};
      module = new vm.SyntheticModule(Object.keys(values), function () {
        for (const [name, value] of Object.entries(values)) this.setExport(name, value);
      }, {context, identifier: file});
    } else if (file === path.join(jsRoot, 'ui.js')) {
      module = new vm.SourceTextModule(`
export {dataStateHtml, renderDataState} from './components/data_state.js';
export function setActiveNav() {}
export function openBottomSheet() { throw new Error('Tests must supply their own sheet'); }
export function closeBottomSheet() {}
`, {context, identifier: file});
    } else if (file === path.join(jsRoot, 'nav_growth_explanation.js')) {
      module = new vm.SyntheticModule(['renderNavGrowthExplanationPanel'], function () {
        this.setExport('renderNavGrowthExplanationPanel', overrides.renderExplanation || (() => {}));
      }, {context, identifier: file});
    } else {
      module = new vm.SourceTextModule(readFileSync(file, 'utf8'), {context, identifier: file});
    }
    cache.set(file, module);
    return module;
  }
  const module = getModule(path.join(jsRoot, entry));
  await module.link((specifier, parent) => getModule(resolve(parent.identifier, specifier)));
  await module.evaluate();
  return module.namespace;
}
