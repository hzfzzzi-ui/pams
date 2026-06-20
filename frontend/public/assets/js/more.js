import { setActiveNav } from './ui.js?v=20260620-design-system-v2';
import { setText } from './utils.js?v=20260607-shared-format-v1';

function todayText() {
  return new Date().toISOString().slice(0, 10);
}

function boot() {
  setActiveNav();
  setText('asof', todayText());
}

boot();
