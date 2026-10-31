// Chart-specific tests now import the actual controller and presenters directly,
// without evaluating a page entry or copying any financial implementation.
export const chartHarnessSource = `
import {createInvestmentChartController} from './features/investment/charts/controller.js';
import {normalizeInvestmentNavRows, buildInvestmentNavChartData, filterReturnWindowRows} from './features/investment/charts/legacy_model.js';
import {esc, formatNumber, formatPercentRate, fmtCny, fmtSignedCny, fmtSignedPctFromRate} from './features/investment/holdings/summary_legacy_model.js';
import {createSummaryView} from './features/investment/holdings/summary_view.js';
const view = createSummaryView({document,host:window,showDetails(){},scope:{listen(){}}});
let controller,disposed=false;
function current() {
  if(disposed)return null;
  return controller ||= createInvestmentChartController({root:document,host:window,
    presenters:{esc,formatNumber,formatPercentRate,fmtCny,fmtSignedCny,fmtSignedPctFromRate,
      statusBadge:view.statusBadge,TopContributionPanel:view.TopContributionPanel},ChartClass:globalThis.Chart});
}
function loadInvestmentNavChartOnce(){return current()?.loadNavOnce()||Promise.resolve(null);}
function renderInvestmentReturnChart(){return current()?.renderReturns()||Promise.resolve(null);}
function getInvestmentChartState(){return current()?.state||null;}
function applyInvestmentReturnWindow(chart,state,key,persist=true){return current()?.applyWindow(chart,state,key,persist);}
function disposeInvestmentCharts(){disposed=true;controller?.dispose();}
export {loadInvestmentNavChartOnce,renderInvestmentReturnChart,getInvestmentChartState,applyInvestmentReturnWindow,
  disposeInvestmentCharts,createInvestmentChartController,normalizeInvestmentNavRows,buildInvestmentNavChartData,filterReturnWindowRows};
`;
