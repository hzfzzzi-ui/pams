import { jget, fmt2 } from './js/api.js';
import { setActiveNav } from './js/ui.js';

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
    const stats = await jget(`/api/transactions/monthly/${currentMonth}`);

    // 更新 KPI
    document.getElementById('kpiIncome').textContent = formatAmount(stats.total_income);
    document.getElementById('kpiExpense').textContent = formatAmount(stats.total_expense);

    const netEl = document.getElementById('kpiNet');
    netEl.textContent = formatAmount(stats.net);
    netEl.className = 'kpi-value net ' + (stats.net >= 0 ? 'positive' : 'negative');

    // 渲染分类饼图
    renderCategoryChart(stats.by_category || []);

    // 渲染按月趋势图
    await renderTrendChart();

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

// 渲染按月趋势折线图
async function renderTrendChart() {
  try {
    // 获取近6个月的数据
    const months = getRecentMonths(6);
    const monthlyData = await Promise.all(
      months.map(month => jget(`/api/transactions/monthly/${month}`).catch(() => ({
        month,
        total_income: 0,
        total_expense: 0,
        net: 0
      })))
    );

    const canvas = document.getElementById('trendChart');

    if (window.__trendChart) {
      window.__trendChart.destroy();
    }

    window.__trendChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: months,
        datasets: [
          {
            label: '收入',
            data: monthlyData.map(d => d.total_income || 0),
            borderColor: '#43d19e',
            backgroundColor: 'rgba(67, 209, 158, 0.1)',
            tension: 0.3,
            fill: true
          },
          {
            label: '支出',
            data: monthlyData.map(d => d.total_expense || 0),
            borderColor: '#ff6b6b',
            backgroundColor: 'rgba(255, 107, 107, 0.1)',
            tension: 0.3,
            fill: true
          },
          {
            label: '结余',
            data: monthlyData.map(d => d.net || 0),
            borderColor: '#6aa6ff',
            backgroundColor: 'rgba(106, 166, 255, 0.1)',
            tension: 0.3,
            fill: true,
            borderDash: [5, 5]
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'top',
            labels: {
              color: '#cfe0ff',
              font: { size: 12 },
              padding: 12,
              usePointStyle: true
            }
          },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const label = ctx.dataset.label || '';
                const value = ctx.parsed.y || 0;
                return `${label}: ¥${fmt2(value)}`;
              }
            }
          }
        },
        scales: {
          x: {
            ticks: { color: '#8ea0c7' },
            grid: { color: 'rgba(36, 48, 85, 0.35)' }
          },
          y: {
            ticks: {
              color: '#8ea0c7',
              callback: (value) => '¥' + fmt2(value)
            },
            grid: { color: 'rgba(36, 48, 85, 0.35)' }
          }
        }
      }
    });
  } catch (err) {
    console.error('渲染趋势图失败:', err);
  }
}

// 获取最近N个月份
function getRecentMonths(n) {
  const months = [];
  const [year, month] = currentMonth.split('-').map(Number);

  for (let i = n - 1; i >= 0; i--) {
    const date = new Date(year, month - 1 - i, 1);
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    months.push(`${y}-${m}`);
  }

  return months;
}

// 加载最近交易
async function loadRecentTransactions() {
  try {
    const transactions = await jget('/api/transactions/recent?limit=30');

    const tbody = document.querySelector('#transactionTable tbody');
    tbody.innerHTML = '';

    if (!transactions || transactions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#8ea0c7;padding:20px;">暂无交易记录</td></tr>';
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
        <td>${tx.currency || 'CNY'}</td>
        <td>${tx.account || 'Santander'}</td>
        <td style="text-align:right" class="${amountClass}">${amountText}</td>
      `;
      tbody.appendChild(row);
    });

    document.getElementById('txNote').textContent = `共 ${transactions.length} 条记录`;

  } catch (err) {
    showError('加载交易记录失败: ' + err.message);
  }
}

// 添加交易
async function addTransaction() {
  const amount = parseFloat(document.getElementById('txAmount').value);
  const type = document.getElementById('txType').value;
  const currency = document.getElementById('txCurrency').value;
  const account = document.getElementById('txAccount').value;
  const category = document.getElementById('txCategory').value.trim();
  const description = document.getElementById('txDescription').value.trim();

  // 验证
  if (!amount || amount <= 0) {
    showAddTxMessage('请输入有效金额', 'error');
    return;
  }

  if (!category) {
    showAddTxMessage('请输入分类', 'error');
    return;
  }

  // 构造交易数据
  const transaction = {
    amount: type === 'expense' ? -Math.abs(amount) : Math.abs(amount),
    currency: currency,
    account: account,
    category: category,
    description: description || null,
    date: new Date().toISOString().split('T')[0]
  };

  try {
    // 提交到后端
    const response = await fetch('/api/transactions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(transaction)
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    showAddTxMessage('✓ 添加成功', 'success');

    // 清空表单
    document.getElementById('txAmount').value = '';
    document.getElementById('txCategory').value = '';
    document.getElementById('txDescription').value = '';

    // 刷新数据
    await loadMonthData();

  } catch (err) {
    showAddTxMessage('添加失败: ' + err.message, 'error');
  }
}

// 显示添加交易的消息
function showAddTxMessage(msg, type) {
  const msgEl = document.getElementById('addTxMsg');
  msgEl.textContent = msg;
  msgEl.style.color = type === 'success' ? '#43d19e' : '#ff6b6b';

  // 3秒后清除消息
  setTimeout(() => {
    msgEl.textContent = '';
  }, 3000);
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

  // 绑定添加交易按钮
  document.getElementById('btnAddTransaction').addEventListener('click', addTransaction);

  // 支持回车提交
  document.getElementById('txAmount').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') addTransaction();
  });
  document.getElementById('txCategory').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') addTransaction();
  });
  document.getElementById('txDescription').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') addTransaction();
  });

  // 加载数据
  await loadMonthData();
}

main().catch(err => {
  showError('初始化失败: ' + String(err));
});
