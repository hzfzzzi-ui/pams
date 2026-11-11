import { escapeHtml } from '../../utils.js';

const dayLabel = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', timeZone: 'UTC' });
const weekdayLabel = new Intl.DateTimeFormat('zh-CN', { weekday: 'short', timeZone: 'UTC' });

export function renderLedgerDays(rows, dateOf, renderRow, { countUnit = '笔', partialLastDay = false } = {}) {
  const days = new Map();
  rows.forEach((row, index) => {
    const date = dateOf(row);
    if (!days.has(date)) days.set(date, []);
    days.get(date).push(renderRow(row, index));
  });
  return Array.from(days, ([date, markup], index) => {
    const day = new Date(date + 'T00:00:00Z');
    const title = Number.isFinite(day.valueOf())
      ? `<time datetime="${escapeHtml(date)}">${dayLabel.format(day)}</time> <small>${weekdayLabel.format(day)}</small>`
      : '日期未知';
    const count = partialLastDay && index === days.size - 1 ? '已加载 ' : '';
    return `<section class="ledgerStreamDay">
      <h3 class="ledgerStreamDayHeader">
        <span>${title}</span>
        <small>${count}${markup.length} ${countUnit}</small>
      </h3>
      ${markup.join('')}
    </section>`;
  }).join('');
}
