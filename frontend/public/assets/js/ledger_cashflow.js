import { mountLedgerPage } from './features/ledger/page.js';

const ledger = mountLedgerPage();

export function disposeLedger() {
  ledger.dispose();
}
