// A view reads only descendants of its explicit root. Null roots are safe for
// optional views; Document roots keep the former document-id lookup behavior.
export function createDomView(root) {
  const query = selector => root?.querySelector?.(selector) || null;
  const byId = id => {
    if (typeof root?.getElementById === 'function') return root.getElementById(id);
    const escaped = String(id).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    return query(`[id="${escaped}"]`);
  };
  return {
    query, byId,
    setText(id, value) {const el = byId(id); if (el) el.textContent = value;},
    showError(error, id = 'err') {
      const el = byId(id);
      if (el) {el.style.display = 'block'; el.textContent = error?.message || String(error);}
    },
    clearError(id = 'err') {
      const el = byId(id);
      if (el) {el.style.display = 'none'; el.textContent = '';}
    },
  };
}
