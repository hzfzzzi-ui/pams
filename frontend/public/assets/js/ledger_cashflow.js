import { mountLedgerPage } from './features/ledger/page.js?v=20261101-ledger-colors';

const ledger = mountLedgerPage();

export function disposeLedger() {
  ledger.dispose();
}
