import { createDomView } from '../../shared/dom_view.js?v=20261031-modules-v1';
// One root owns its queries/listeners; sheet sessions are explicit child views.
export function createLedgerDom(root) {
  const view = createDomView(root);
  return {$: view.byId, ...view};
}

export function createLedgerEvents(current) {
  const groups = new Map();
  function clear(group) {
    for (const remove of groups.get(group) || []) remove();
    groups.delete(group);
  }
  return {
    listen(group, target, type, callback, options) {
      if (!target || !current()) return;
      const listener = event => { if (current()) return callback(event); };
      target.addEventListener(type, listener, options);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(() => target.removeEventListener(type, listener, options));
    },
    clear,
    dispose() { for (const key of [...groups.keys()]) clear(key); },
  };
}

export function createLedgerSheets(service, events, current) {
  let generation = 0;
  let session = null;
  function close() {
    generation += 1;
    events.clear('sheet');
    service.close();
    session = null;
  }
  return {
    open(title, html) {
      if (!current()) return null;
      generation += 1;
      events.clear('sheet');
      const token = generation;
      const body = service.open(title, html);
      const active = () => current() && token === generation && service.opened && body.isConnected;
      session = {
        body,
        active,
        listen(target, type, callback, options) {
          events.listen('sheet', target, type, event => { if (active()) return callback(event); }, options);
        },
        close() { if (active()) close(); },
      };
      return session;
    },
    close,
    dispose() {
      generation += 1;
      events.clear('sheet');
      session = null;
      service.dispose({ restoreFocus: false });
    },
  };
}
