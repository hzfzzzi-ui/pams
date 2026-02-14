import { jget, fmt2 } from './api.js';
import { setActiveNav } from './ui.js';

// 当前月份 (YYYY-MM)
let currentMonth = new Date().toISOString().slice(0, 7);

// 格式化金额
function formatAmount(amount) {
  return '¥' + fmt2(amount);
}

// 加载月度数据
async function loadMonthData() {
  try {
    // 获取月度统计
    const stats = await jget(`/transactions/monthly/${currentMonth}`);

    // 更新 KPI
    document.getElementById('kpiIncome').textContent = formatAmount(stats.total_income);
    document.getElementById('kpiExpense').textContent = formatAmount(stats.total_expense);

    const netEl = document.getElementById('kpiNet');
    netEl.textContent = formatAmount(stats.net);
    netEl.className = 'kpi-value net ' + (stats.net >= 0 ? 'positive' : 'negative');

    // 渲染分类饼图
    renderCategoryChart(stats.by_category || []);

    // 加载最近交易
    await loadRecentTransactions();

  } catch (err) {
    showError('加载月度数据失败: ' + err.message);
  }
}

// 渲染分类饼图
function renderCategoryChart(categories) {
  const canvas = document.getElementById('categoryChart');

  if (window.__categoryChart) {
    window.__categoryChart.destroy();
  }

  if (!categories || categories.length === 0) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#8ea0c7';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('本月暂无支出数据', canvas.width / 2, canvas.height / 2);
    return;
  }

  const colors = [
    '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0',
    '#9966FF', '#FF9F40', '#FF6384', '#C9CBCF'
  ];

  window.__categoryChart = new Chart(canvas, {
    type: 'pie',
    data: {
      labels: categories.map(c => c.category),
      datasets: [{
        data: categories.map(c => c.amount),
        backgroundColor: colors.slice(0, categories.length),
        borderColor: '#0b0f14',
        borderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'right',
          labels: {
            color: '#cfe0ff',
            font: { size: 12 },
            padding: 12
          }
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const label = ctx.label || '';
              const value = ctx.parsed || 0;
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const percentage = ((value / total) * 100).toFixed(1);
              return `${label}: ¥${fmt2(value)} (${percentage}%)`;
            }
          }
        }
      }
    }
  });
}

// 加载最近交易
async function loadRecentTransactions() {
  try {
    const transactions = await jget('/transactions/recent?limit=30');

    const tbody = document.querySelector('#transactionTable tbody');
    tbody.innerHTML = '';

    if (!transactions || transactions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#8ea0c7;padding:20px;">暂无交易记录</td></tr>';
      document.getElementById('txNote').textContent = '';
      return;
    }

    transactions.forEach(tx => {
      const row = document.createElement('tr');
      const amountClass = tx.amount >= 0 ? 'amount-income' : 'amount-expense';
      const amountText = tx.amount >= 0 ? '+' + formatAmount(tx.amount) : '-' + formatAmount(Math.abs(tx.amount));

      row.innerHTML = `
        <td>${tx.date}</td>
        <td>${tx.category}${tx.subcategory ? ' · ' + tx.subcategory : ''}</td>
        <td>${tx.description || '-'}</td>
        <td>${tx.account || 'manual'}</td>
        <td style="text-align:right" class="${amountClass}">${amountText}</td>
      `;
      tbody.appendChild(row);
    });

    document.getElementById('txNote').textContent = `共 ${transactions.length} 条记录`;

  } catch (err) {
    showError('加载交易记录失败: ' + err.message);
  }
}

// 显示错误
function showError(msg) {
  const errEl = document.getElementById('err');
  errEl.textContent = msg;
  errEl.style.display = 'block';
}

// 月份切换
function changeMonth(delta) {
  const [year, month] = currentMonth.split('-').map(Number);
  const date = new Date(year, month - 1 + delta, 1);
  currentMonth = date.toISOString().slice(0, 7);
  document.getElementById('currentMonth').textContent = currentMonth;
  loadMonthData();
}

// 初始化
async function main() {
  setActiveNav();

  // 设置当前月份
  document.getElementById('currentMonth').textContent = currentMonth;

  // 绑定月份切换按钮
  document.getElementById('prevMonth').addEventListener('click', () => changeMonth(-1));
  document.getElementById('nextMonth').addEventListener('click', () => changeMonth(1));

  // 加载数据
  await loadMonthData();
}

main().catch(err => {
  showError('初始化失败: ' + String(err));
});
