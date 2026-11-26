export function createState() {
const state = {
  accounts: [],
  categories: [],
  sources: [],
  assets: [],
  reviewOptions: {},
  filters: null,
  rows: [],
  summary: {},
  attribution: null,
  attributionLoadPromise: null,
  campaigns: null,
  campaignList: null,
  viewMode: 'list',
  showAll: false,
};
return state;
}
