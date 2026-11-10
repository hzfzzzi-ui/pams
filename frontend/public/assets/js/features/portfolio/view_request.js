import {createPageRead} from '../../shared/page_read.js';

export const loadPanels = createPageRead(() => '/api/portfolio/risk-and-config?baseCurrency=CNY');
