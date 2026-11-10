import {jget} from './api.js';
import {afterInvestmentStartup} from './investment_startup.js';
import {mount} from './features/portfolio/risk_controller.js';
import {loadPanels} from './features/portfolio/view_request.js';

// Compatibility entry for the existing investment document.
const root = document.getElementById('portfolio-risk-disclosure');
if (root) {
  let controller;
  const resume = () => {
    controller = mount(root, {get: jget, afterStartup: afterInvestmentStartup, loadPanels, state: controller?.snapshot()});
  };
  resume();
  // BFCache restores the document without evaluating its entry modules again.
  window.addEventListener('pageshow', event => {
    if (event.persisted && root.isConnected && controller.disposed) resume();
  });
}
