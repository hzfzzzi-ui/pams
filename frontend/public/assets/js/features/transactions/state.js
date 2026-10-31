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
  showAll: false,
};
return state;
}
