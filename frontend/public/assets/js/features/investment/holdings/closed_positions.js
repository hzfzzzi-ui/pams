const roots = new WeakMap();

// Both investment controllers share one binding; the last owner releases it.
export function acquireClosedPositions(root, host) {
  let owner = roots.get(root);
  if (!owner) {
    const showClosedPositions = () => new URLSearchParams(host.location.search).get('show_closed') === '1';
    const withClosedParam = path => {
      if (!showClosedPositions()) return path;
      const separator = path.includes('?') ? '&' : '?';
      return `${path}${separator}include_closed=true`;
    };
    const toggles = [...root.querySelectorAll('.showClosedPositionsToggle')];
    const legacy = root.querySelector('#showClosedPositions');
    if (!toggles.length && legacy) toggles.push(legacy);
    const changes = toggles.map(el => {
      el.checked = showClosedPositions();
      const changed = () => {
        const checked = el.checked;
        for (const other of toggles) other.checked = checked;
        const url = new URL(host.location.href);
        if (checked) url.searchParams.set('show_closed', '1'); else url.searchParams.delete('show_closed');
        host.location.href = url.toString();
      };
      el.addEventListener('change', changed);
      return () => el.removeEventListener('change', changed);
    });
    owner = {count: 0, showClosedPositions, withClosedParam, dispose: () => changes.forEach(remove => remove())};
    roots.set(root, owner);
  }
  owner.count++;
  let released = false;
  return {
    showClosedPositions: owner.showClosedPositions,
    withClosedParam: owner.withClosedParam,
    release() {
      if (released) return;
      released = true;
      if (--owner.count === 0) { owner.dispose(); roots.delete(root); }
    },
  };
}
