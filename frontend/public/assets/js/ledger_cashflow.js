import { mountLedgerPage } from './features/ledger/page.js?v=20261031-ledger-modules-v1';

const ledger = mountLedgerPage();

export function disposeLedger() {
  ledger.dispose();
}
