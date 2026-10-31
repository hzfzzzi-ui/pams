import {jget} from './api.js?v=20261031-modules-v1';
import {openBottomSheet, closeBottomSheet} from './ui.js?v=20261031-modules-v1';
import {afterInvestmentStartup} from './investment_startup.js?v=20261031-modules-v1';
import {mount} from './features/portfolio/config_controller.js?v=20261031-modules-v1';

// Compatibility entry for the existing investment document.
const root = document.getElementById('portfolio-constraints');
if (root) {
  let controller;
  const resume = () => {
    controller = mount(root, {get: jget, afterStartup: afterInvestmentStartup, showDetails: openBottomSheet, closeDetails: closeBottomSheet, state: controller?.snapshot()});
  };
  resume();
  window.addEventListener('pageshow', event => {
    if (event.persisted && root.isConnected && controller.disposed) resume();
  });
}
