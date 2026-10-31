import {jget} from './api.js?v=20261031-modules-v1';
import {afterInvestmentStartup} from './investment_startup.js?v=20261031-modules-v1';
import {mount} from './features/portfolio/risk_controller.js?v=20261031-modules-v1';

// Compatibility entry for the existing investment document.
const root = document.getElementById('portfolio-risk-disclosure');
if (root) {
  let controller;
  const resume = () => {
    controller = mount(root, {get: jget, afterStartup: afterInvestmentStartup, state: controller?.snapshot()});
  };
  resume();
  // BFCache restores the document without evaluating its entry modules again.
  window.addEventListener('pageshow', event => {
    if (event.persisted && root.isConnected && controller.disposed) resume();
  });
}
