import { setActiveNav } from './ui.js?v=20260622-data-state-a11y-v1';
import { setText } from './utils.js?v=20260607-shared-format-v1';

function todayText() {
  return new Date().toISOString().slice(0, 10);
}

function boot() {
  setActiveNav();
  setText('asof', todayText());
}

boot();
