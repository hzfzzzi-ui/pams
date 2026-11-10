import {jget} from './api.js';
import {openBottomSheet, closeBottomSheet} from './ui.js';
import {afterInvestmentStartup} from './investment_startup.js';
import {mount} from './features/portfolio/config_controller.js';
import {loadPanels} from './features/portfolio/view_request.js';

// Compatibility entry for the existing investment document.
const root = document.getElementById('portfolio-constraints');
if (root) {
  let controller;
  const resume = () => {
    controller = mount(root, {get: jget, afterStartup: afterInvestmentStartup, loadPanels,
      showDetails: openBottomSheet, closeDetails: closeBottomSheet, state: controller?.snapshot()});
  };
  resume();
  window.addEventListener('pageshow', event => {
    if (event.persisted && root.isConnected && controller.disposed) resume();
  });
}
