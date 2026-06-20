import { jget, jpost, fmtMoneyFixed2, fmtSignedMoneyFixed2 } from "./api.js?v=20260606-refresh-v3";
import { openBottomSheet, setActiveNav } from "./ui.js?v=20260620-data-state-v1";

let config = null;
let selectedProfile = null;
let latestPreview = null;
let accountOverview = null;
const collapsedAccountGroups = new Set();

function showError(msg) {
  const el = document.getElementById("err");
  el.textContent = msg;
  el.style.display = "block";
}

function clearError() {
  const el = document.getElementById("err");
  el.textContent = "";
  el.style.display = "none";
}

function setMsg(msg) {
  document.getElementById("opMsg").textContent = msg || "";
}

function profileCurrency(code) {
  return config?.profiles?.[code]?.currency || "CNY";
}

function money(value, currency = "CNY") {
  return fmtMoneyFixed2(value, currency);
}

function moneyOrDash(value, currency = "CNY") {
  return value == null || value === "" ? "—" : money(value, currency);
}

function signedMoney(value, currency = "CNY") {
  return fmtSignedMoneyFixed2(value, currency);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function moneyAmount(value) {
  if (value == null || value === "") return "—";
  const n = Number(value || 0);
  return `${n < 0 ? "-" : ""}${Math.abs(n).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function moneyAmountObj(obj) {
  if (!obj || obj.amount == null || obj.amount === "") return "—";
  return moneyAmount(obj.amount);
}

function primaryBalanceCurrency(account) {
  const balance = account.primary_balance || {};
  if (balance.amount == null || balance.amount === "") {
    return (account.platform_balances || []).length
      ? esc(account.platform_balances[0].currency || account.base_currency || "CNY")
      : "—";
  }
  return esc(balance.currency || account.base_currency || "CNY");
}

function balanceAmountPills(rows, valueKey = "market_value") {
  const items = rows || [];
  if (!items.length) return "—";
  return `<div class="balanceBreakdown">${items.map((r) => `
    <span class="balancePill">${moneyAmount(r[valueKey])}</span>
  `).join("")}</div>`;
}

function statusChip(status, label) {
  return `<span class="statusChip ${esc(status || "")}">${esc(label || status || "-")}</span>`;
}

function basisBadge(label) {
  return `<span class="basisBadge">${esc(label || "-")}</span>`;
}

function reviewActionCell(account) {
  const code = esc(account.account_code);
  if (account.status === "ready_for_review") {
    return `
      <div class="reviewActions">
        <button class="reviewBtn primary" data-account-review="${code}" data-review-action="approve" type="button">确认</button>
      </div>
    `;
  }
  if (["missing_statement", "missing_platform", "missing_manual", "missing_data"].includes(account.status)) {
    return `
      <div class="reviewActions">
        <button class="reviewBtn warn" data-account-review="${code}" data-review-action="defer_missing" type="button">暂缓</button>
      </div>
    `;
  }
  if (account.status === "approved") {
    const reviewedAt = account.latest_review?.reviewed_at ? ` title="${esc(account.latest_review.reviewed_at)}"` : "";
    return `<span class="miniBadge ok"${reviewedAt}>已确认</span>`;
  }
  if (account.status === "missing_deferred") {
    const reviewedAt = account.latest_review?.reviewed_at ? ` title="${esc(account.latest_review.reviewed_at)}"` : "";
    return `<span class="miniBadge pending"${reviewedAt}>已暂缓</span>`;
  }
  if (account.review_status === "stale") {
    return `<span class="miniBadge warn">需复核</span>`;
  }
  return `<span class="muted">—</span>`;
}

function accountIssueKind(account) {
  const status = String(account.status || "");
  if (status.startsWith("missing")) return "missing";
  if (Math.abs(accountDiffAmount(account)) > 1) return "diff";
  if (account.review_status === "stale") return "stale";
  if (Number(account.primary_balance || 0) < 0 && account.account_nature !== "credit_card") return "negative";
  if (status === "in_transit") return "transit";
  return "normal";
}

function accountIssueLabel(account) {
  const kind = accountIssueKind(account);
  return ({
    missing: "缺数据",
    diff: "金额差异",
    stale: "日期过期",
    negative: "负余额",
    transit: "在途资金",
    normal: "",
  })[kind] || "";
}

function compactStatusCell(account) {
  const issue = accountIssueLabel(account);
  if (!issue) return '<span class="muted">—</span>';
  const tone = accountIssueKind(account) === "missing" || accountIssueKind(account) === "negative" ? "warn" : "pending";
  return `<span class="miniBadge ${tone}" title="${esc(account.status_label || issue)}">${esc(issue)}</span>`;
}

function accountDetailGrid(account) {
  return `
    <div class="accountDetailGrid">
      <div><strong>核准依据</strong><span>${basisBadge(account.balance_basis_label || account.truth_source_label || "-")}</span></div>
      <div><strong>核准重点</strong><span>${esc(account.review_hint || account.role || "-")}</span></div>
      <div><strong>审批状态</strong><span>${reviewActionCell(account)}</span></div>
      <div><strong>数据来源</strong><span>${esc(account.source || account.statement_source || account.platform || "-")}</span></div>
      <div><strong>账单/官方</strong><span class="mono">${moneyAmountObj(account.statement_balance)}</span></div>
      <div><strong>平台/持仓</strong><span class="mono">${account.platform_balance ? moneyAmountObj(account.platform_balance) : "见余额矩阵"}</span></div>
      <div><strong>手动/在途</strong><span class="mono">${moneyAmountObj(account.manual_balance)}</span></div>
      <div><strong>当前采用</strong><span class="mono">${moneyAmount(account.primary_balance?.amount)}</span></div>
      ${ownerAdjustedNote(account) ? `<div class="wide"><strong>口径</strong><span>${ownerAdjustedNote(account)}</span></div>` : ""}
      ${account.manual_balance?.note ? `<div class="wide"><strong>备注</strong><span>${esc(account.manual_balance.note)}</span></div>` : ""}
      ${account.review_status === "stale" ? `<div class="wide"><strong>过期原因</strong><span>${esc(account.review_stale_reason || "审批已过期")}</span></div>` : ""}
    </div>
  `;
}

function accountDetailCell(account) {
  const issue = accountIssueLabel(account) || account.review_hint || "详情";
  return `
    <details class="compactDetails accountRowDetails">
      <summary>${esc(issue)}</summary>
      ${accountDetailGrid(account)}
    </details>
  `;
}

function amountTone(amount) {
  const n = Number(amount || 0);
  if (n < 0) return "down";
  if (n > 0) return "";
  return "";
}

function amountSpan(amount) {
  const tone = amountTone(amount);
  return `<span class="mono ${tone}">${moneyAmount(amount)}</span>`;
}

function primaryBalanceAmountDisplay(account) {
  if (account.primary_balance) return amountSpan(account.primary_balance.amount);
  if ((account.platform_balances || []).length) return balanceAmountPills(account.platform_balances, "market_value");
  return "—";
}

function ownerAdjustedNote(account) {
  const balance = account.primary_balance || {};
  if (!balance.gross_amount || !balance.owner_ratio) return "";
  return `<div class="muted">平台总额 ${esc(balance.currency || account.base_currency || "CNY")} ${moneyAmount(balance.gross_amount)} × ${balance.owner_ratio} 已按个人资金口径计入</div>`;
}

function balanceSummaryEntries(accounts) {
  const totals = new Map();
  for (const account of accounts || []) {
    const balance = account.primary_balance || {};
    if (balance.amount == null || balance.amount === "") continue;
    const currency = balance.currency || account.base_currency || "CNY";
    totals.set(currency, (totals.get(currency) || 0) + Number(balance.amount || 0));
  }
  return [...totals.entries()];
}

function balanceSummaryAmount(accounts) {
  const entries = balanceSummaryEntries(accounts);
  if (!entries.length) return "—";
  if (entries.length > 1) return `<span class="muted">见子账户</span>`;
  return amountSpan(entries[0][1]);
}

function accountDiffAmount(account) {
  const values = [
    account.statement_balance?.amount,
    account.platform_balance?.amount,
    account.manual_balance?.amount,
    account.primary_balance?.amount,
  ].filter(v => v != null && v !== "").map(v => Number(v));
  if (values.length < 2) return 0;
  return Math.max(...values) - Math.min(...values);
}

function totalDiffAmount(accounts) {
  return (accounts || []).reduce((sum, account) => sum + Math.abs(accountDiffAmount(account)), 0);
}

function staleCount(accounts) {
  return (accounts || []).filter(account => account.review_status === "stale").length;
}

function negativeAssetAccounts(accounts) {
  return (accounts || []).filter((account) => {
    const value = Number(account.primary_balance?.amount || 0);
    return value < 0 && account.account_type !== "liability";
  });
}

function buildAccountIssueQueue(accounts, summary = {}) {
  const missing = (accounts || []).filter(account => String(account.status || "").startsWith("missing"));
  const diffRows = (accounts || []).filter(account => Math.abs(accountDiffAmount(account)) > 1);
  const stale = (accounts || []).filter(account => account.review_status === "stale");
  const negativeAssets = negativeAssetAccounts(accounts);
  const transit = (accounts || []).filter(account => account.status === "in_transit");
  const missingCount = missing.length || summary.missing_data || 0;
  return [
    { title: "缺数据", count: missingCount, tone: missingCount ? "risk" : "data", detail: missing.slice(0, 2).map(a => a.subaccount_label || a.display_name).join(" / ") || "当前无阻塞项" },
    { title: "金额差异", count: diffRows.length, tone: diffRows.length ? "warn" : "data", detail: diffRows.length ? `合计差异 ${money(totalDiffAmount(accounts))}` : "账单/平台/采用值一致" },
    { title: "日期过期", count: stale.length || summary.stale_review || 0, tone: stale.length ? "warn" : "data", detail: stale.slice(0, 2).map(a => a.subaccount_label || a.display_name).join(" / ") || "核准日期正常" },
    { title: "负余额", count: negativeAssets.length, tone: negativeAssets.length ? "risk" : "data", detail: negativeAssets.slice(0, 2).map(a => a.subaccount_label || a.display_name).join(" / ") || "信用卡负债已按属性处理" },
    { title: "在途资金", count: transit.length || summary.in_transit || 0, tone: transit.length || summary.in_transit ? "warn" : "data", detail: transit.slice(0, 2).map(a => a.subaccount_label || a.display_name).join(" / ") || "当前无新增在途" },
  ];
}

function renderAccountIssueQueue(accounts, summary) {
  const el = document.getElementById("accountIssueQueue");
  if (!el) return;
  el.innerHTML = buildAccountIssueQueue(accounts, summary).map(item => `
    <button class="issueFilterChip ${item.tone}" type="button" data-account-filter="${esc(item.title)}" title="${esc(item.detail)}">
      <span>${esc(item.title)}</span><span class="mono">${item.count}</span>
    </button>
  `).join("");
}

function accountTrees(accounts) {
  const roots = new Map();
  for (const account of accounts || []) {
    const key = account.account_root_code || account.parent_account_code || account.account_code;
    if (!roots.has(key)) {
      roots.set(key, {
        key,
        label: account.account_root_label || account.institution || account.display_name || key,
        order: Number(account.account_root_order || 999),
        accounts: [],
      });
    }
    roots.get(key).accounts.push(account);
  }
  return [...roots.values()].sort((a, b) => a.order - b.order || a.label.localeCompare(b.label, "zh-CN"));
}

function summaryBalanceValue(row) {
  return row.net_asset_balance == null || row.net_asset_balance === ""
    ? row.latest_balance
    : row.net_asset_balance;
}

function rawBalanceNote(row) {
  if (row.balance_source === "holding_snapshot") {
    return '<div class="muted">余额来源: 持仓快照</div>';
  }
  if (row.account_kind !== "credit_card" || row.latest_balance == null || row.latest_balance === "") {
    return "";
  }
  return `<div class="muted">账单金额: ${money(row.latest_balance, profileCurrency(row.profile_code))}（信用卡按负债计入）</div>`;
}

function relationTypeLabel(type) {
  return {
    investment_funding: "投资资金划转",
    benefit_income: "雇主福利入账",
    account_transfer: "银行账户转账",
    credit_card_payment: "信用卡还款",
    payment_proxy: "支付通道",
  }[type] || type || "-";
}

function matchTypeLabel(type) {
  return {
    investment_funding: "内部投资划转",
    benefit_income: "福利收入确认",
    account_transfer: "银行转账匹配",
    credit_card_payment: "信用卡还款匹配",
    payment_proxy: "支付通道映射",
  }[type] || type || "-";
}

function issueTypeLabel(type) {
  return {
    unmatched_row: "未匹配",
    one_sided_match: "缺成对流水",
  }[type] || type || "-";
}

function confidence(value) {
  const n = Number(value || 0);
  return `${(n * 100).toFixed(1)}%`;
}

function compactText(value, max = 96) {
  const text = String(value || "-").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function profileLabel(code) {
  return config?.profiles?.[code]?.label || code;
}

function initAccountTabs() {
  const buttons = document.querySelectorAll("[data-account-tab]");
  const panels = document.querySelectorAll("[data-account-panel]");
  const show = (tab) => {
    buttons.forEach((btn) => btn.classList.toggle("active", btn.dataset.accountTab === tab));
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.accountPanel !== tab;
    });
    if (location.hash !== `#${tab}`) {
      history.replaceState(null, "", `${location.pathname}${location.search}#${tab}`);
    }
  };
  buttons.forEach((btn) => btn.addEventListener("click", () => show(btn.dataset.accountTab)));
  const initial = location.hash ? location.hash.slice(1) : "overview";
  show(["overview", "balance", "transfers", "sources"].includes(initial) ? initial : "overview");
}

function renderAccountKpis(data) {
  const summary = data?.summary || {};
  const accounts = data?.accounts || [];
  const diffAmount = totalDiffAmount(accounts);
  const stale = staleCount(accounts);
  const ready = Number(summary.ready_for_review || 0);
  const missing = Number(summary.missing_data || 0);
  const status = ready || missing || stale ? "待核准" : "已核准";
  const el = document.getElementById("accountKpis");
  if (!el) return;
  el.innerHTML = `
    <div class="metricPanel">
      <div class="label">核准状态</div>
      <div class="value">${status}</div>
    </div>
    <div class="metricPanel">
      <div class="label">差异金额</div>
      <div class="value">${money(diffAmount)}</div>
    </div>
    <div class="metricPanel">
      <div class="label">待核准账户</div>
      <div class="value">${summary.ready_for_review || 0}</div>
    </div>
    <div class="metricPanel">
      <div class="label">过期数据</div>
      <div class="value">${stale || summary.stale_review || 0}</div>
    </div>
    <div class="metricPanel">
      <div class="label">在途资金</div>
      <div class="value">${summary.in_transit || 0}</div>
    </div>
    <div class="metricPanel">
      <div class="label">缺数据</div>
      <div class="value">${summary.missing_data || 0}</div>
    </div>
  `;
}

function renderAccountMobileHero(data) {
  const el = document.getElementById("accountMobileHero");
  if (!el) return;
  const summary = data?.summary || {};
  const accounts = data?.accounts || [];
  const diffAmount = totalDiffAmount(accounts);
  const stale = staleCount(accounts) || Number(summary.stale_review || 0);
  const ready = Number(summary.ready_for_review || 0);
  const missing = Number(summary.missing_data || 0);
  const pendingTotal = ready + stale + missing;
  el.innerHTML = `
    <div class="accountMobileLabel">待处理账户</div>
    <div class="accountMobileValue ${pendingTotal ? "down" : "up"}">${pendingTotal}</div>
    <div class="accountMobileStats">
      <div class="accountMobileStat">
        <span>差异金额</span>
        <strong>${money(diffAmount)}</strong>
      </div>
      <div class="accountMobileStat">
        <span>在途资金</span>
        <strong>${summary.in_transit || 0}</strong>
      </div>
      <div class="accountMobileStat">
        <span>缺数据</span>
        <strong>${missing}</strong>
      </div>
    </div>
  `;
}

function renderAccountOverviewRows(accounts) {
  const tbody = document.getElementById("accountOverviewRows");
  if (!tbody) return;
  const trees = accountTrees(accounts);
  tbody.innerHTML = trees.map((tree) => `
    <tr class="accountGroupRow" data-group-key="${esc(tree.key)}">
      <td>
        <span class="mono">${collapsedAccountGroups.has(tree.key) ? "▸" : "▾"}</span>
        ${esc(tree.label)}
        <span class="groupMeta">${tree.accounts.length} 个子账户</span>
      </td>
      <td></td>
      <td class="num">${balanceSummaryAmount(tree.accounts)}</td>
      <td colspan="2"></td>
    </tr>
    ${tree.accounts.map((a) => `
      <tr data-parent-group="${esc(tree.key)}" data-account-issue="${accountIssueKind(a)}" class="${collapsedAccountGroups.has(tree.key) ? "accountCollapsed" : ""}">
        <td class="accountCell">
          <div style="font-weight:800">${esc(a.subaccount_label || a.display_name || a.account_code)}</div>
          <div class="muted">${esc(a.account_nature_label || "")}</div>
        </td>
        <td>${primaryBalanceCurrency(a)}</td>
        <td class="num">${primaryBalanceAmountDisplay(a)}</td>
        <td>${compactStatusCell(a)}</td>
        <td class="focusCell">${accountDetailCell(a)}</td>
      </tr>
    `).join("")}
  `).join("") || '<tr><td colspan="5" class="muted" style="text-align:center;padding:20px">暂无账户主数据</td></tr>';
}

function accountMobileRows(accounts) {
  const filtered = (accounts || []).filter((account) => {
    const active = document.body.dataset.accountFilter || "";
    return !active || accountIssueKind(account) === active;
  });
  const issueRows = filtered.filter(account => accountIssueKind(account) !== "normal");
  return (issueRows.length ? issueRows : filtered).slice(0, 80);
}

function renderMobileAccountList(accounts) {
  const el = document.getElementById("mobileAccountList");
  if (!el) return;
  const rows = accountMobileRows(accounts);
  if (!rows.length) {
    el.innerHTML = '<div class="mobileEmpty">暂无账户</div>';
    return;
  }
  el.innerHTML = rows.map((account, index) => {
    const issue = accountIssueLabel(account) || account.status_label || "已核准";
    const amount = account.primary_balance?.amount;
    const amountClass = Number(amount || 0) < 0 ? "down" : "";
    return `
      <button class="mobileAccountRow" type="button" data-mobile-account-index="${index}">
        <span class="mobileAccountMain">
          <strong>${esc(account.subaccount_label || account.display_name || account.account_code)}</strong>
          <small>${esc(account.account_root_label || account.institution || account.account_nature_label || "")}</small>
        </span>
        <span class="mobileAccountValue">
          <b class="mono ${amountClass}">${moneyAmount(amount)}</b>
          <small>${esc(issue)}</small>
        </span>
      </button>
    `;
  }).join("");
  el.querySelectorAll("[data-mobile-account-index]").forEach((node) => {
    node.addEventListener("click", () => {
      const account = rows[Number(node.dataset.mobileAccountIndex)];
      openBottomSheet(account.subaccount_label || account.display_name || "账户详情", accountDetailGrid(account));
    });
  });
}

function renderAccountBalanceRows(accounts) {
  const tbody = document.getElementById("accountBalanceRows");
  if (!tbody) return;
  const trees = accountTrees(accounts);
  tbody.innerHTML = trees.map((tree) => `
    <tr class="accountGroupRow" data-group-key="${esc(tree.key)}">
      <td>
        <span class="mono">${collapsedAccountGroups.has(tree.key) ? "▸" : "▾"}</span>
        ${esc(tree.label)}
        <span class="groupMeta">${tree.accounts.length} 个子账户</span>
      </td>
      <td></td>
      <td></td>
      <td colspan="3"></td>
      <td class="num">${balanceSummaryAmount(tree.accounts)}</td>
      <td colspan="2"></td>
    </tr>
    ${tree.accounts.map((a) => `
      <tr data-parent-group="${esc(tree.key)}" class="${collapsedAccountGroups.has(tree.key) ? "accountCollapsed" : ""}">
        <td class="accountCell">
          <div style="font-weight:800">${esc(a.subaccount_label || a.display_name || a.account_code)}</div>
          <div class="muted">${esc(a.account_nature_label || "")}</div>
        </td>
        <td>${basisBadge(a.balance_basis_label || a.truth_source_label || "-")}</td>
        <td>${primaryBalanceCurrency(a)}</td>
        <td class="num">${moneyAmountObj(a.statement_balance)}</td>
        <td class="num">${a.platform_balance ? moneyAmountObj(a.platform_balance) : balanceAmountPills(a.platform_balances, "market_value")}</td>
        <td class="num">${moneyAmountObj(a.manual_balance)}</td>
        <td class="num">${primaryBalanceAmountDisplay(a)}</td>
        <td>${statusChip(a.status, a.status_label)}</td>
        <td>${reviewActionCell(a)}</td>
      </tr>
    `).join("")}
  `).join("") || '<tr><td colspan="9" class="muted" style="text-align:center;padding:20px">暂无账户余额数据</td></tr>';
}

function renderAccountCenter(data) {
  accountOverview = data;
  renderAccountMobileHero(data);
  renderAccountKpis(data);
  renderAccountIssueQueue(data?.accounts || [], data?.summary || {});
  renderAccountOverviewRows(data?.accounts || []);
  renderAccountBalanceRows(data?.accounts || []);
  renderMobileAccountList(data?.accounts || []);
}

async function loadAccountOverview() {
  const data = await jget("/api/accounts/overview");
  renderAccountCenter(data);
}

async function reviewAccount(accountCode, action) {
  clearError();
  const actionLabel = action === "approve" ? "确认核准" : "暂缓缺数据";
  setMsg(`正在提交${actionLabel}...`);
  const note = action === "approve" ? "账户核准中心确认当前余额" : "账户核准中心暂缓当前缺数据项";
  const data = await jpost(`/api/accounts/${encodeURIComponent(accountCode)}/review`, { action, note });
  renderAccountCenter(data);
  setMsg(`${actionLabel}完成：${accountCode}`);
}

function initAccountReviewActions() {
  document.addEventListener("click", (event) => {
    const filterChip = event.target.closest("[data-account-filter]");
    if (filterChip) {
      const label = filterChip.dataset.accountFilter;
      const map = { "缺数据": "missing", "金额差异": "diff", "日期过期": "stale", "负余额": "negative", "在途资金": "transit" };
      const next = document.body.dataset.accountFilter === map[label] ? "" : map[label];
      document.body.dataset.accountFilter = next || "";
      document.querySelectorAll("[data-account-filter]").forEach(el => el.classList.toggle("active", next && el === filterChip));
      renderMobileAccountList(accountOverview?.accounts || []);
      return;
    }
    const groupRow = event.target.closest(".accountGroupRow[data-group-key]");
    if (groupRow) {
      const key = groupRow.dataset.groupKey;
      if (collapsedAccountGroups.has(key)) collapsedAccountGroups.delete(key);
      else collapsedAccountGroups.add(key);
      renderAccountOverviewRows(accountOverview?.accounts || []);
      renderAccountBalanceRows(accountOverview?.accounts || []);
      return;
    }
    const overviewRow = event.target.closest("#accountOverviewRows tr[data-account-issue]");
    if (overviewRow && !event.target.closest("details")) {
      const details = overviewRow.querySelector(".accountRowDetails");
      if (details) {
        if (window.matchMedia("(max-width: 760px)").matches) {
          const title = overviewRow.querySelector(".accountCell div")?.innerText || "账户详情";
          openBottomSheet(title, details.querySelector(".accountDetailGrid")?.outerHTML || details.innerHTML);
        } else {
          details.open = !details.open;
        }
      }
      return;
    }
    const btn = event.target.closest("[data-account-review]");
    if (!btn) return;
    const accountCode = btn.dataset.accountReview;
    const action = btn.dataset.reviewAction;
    btn.disabled = true;
    reviewAccount(accountCode, action)
      .catch((e) => showError(e.message || String(e)))
      .finally(() => {
        btn.disabled = false;
      });
  });
}

function selectProfile(code) {
  selectedProfile = code;
  const p = config.profiles[code] || {};
  document.getElementById("profileCode").value = code;
  document.getElementById("since").value = p.since || config?.defaults?.since || "2026-01-01";
  document.getElementById("filePaths").value = (p.default_files || []).join("\n");
  document.querySelectorAll(".profileItem").forEach((el) => {
    el.classList.toggle("active", el.dataset.profile === code);
  });
}

function renderProfiles() {
  const el = document.getElementById("profileList");
  const profiles = config?.profiles || {};
  el.innerHTML = Object.entries(profiles).map(([code, p]) => `
    <div class="profileItem" data-profile="${code}">
      <div class="profileTitle">${p.label || code}</div>
      <div class="muted" style="margin-top:4px">${p.bank || "-"} / ${p.account_kind || "-"} / ${p.account_code || "-"}</div>
      <div class="muted">${p.statement_template || "-"}</div>
    </div>
  `).join("");
  el.querySelectorAll(".profileItem").forEach((item) => {
    item.addEventListener("click", () => selectProfile(item.dataset.profile));
  });
  const first = Object.keys(profiles)[0];
  if (first) selectProfile(first);
}

function buildRequest() {
  const files = document.getElementById("filePaths").value
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter(Boolean);
  return {
    profile_code: selectedProfile,
    files,
    since: document.getElementById("since").value || "2026-01-01",
    include_rows: true,
  };
}

function renderSummary(summary) {
  const el = document.getElementById("summaryCards");
  const rows = summary || [];
  if (!rows.length) {
    el.innerHTML = '<div class="muted">暂无预览数据</div>';
    return;
  }
  el.innerHTML = rows.map((r) => {
    const balance = summaryBalanceValue(r);
    const currency = profileCurrency(r.profile_code);
    const balanceStyle = Number(balance || 0) < 0 ? ' style="color:var(--bad)"' : "";
    return `
      <div class="metricPanel">
        <div class="label">${profileLabel(r.profile_code)}</div>
        <div class="value"${balanceStyle}>${moneyOrDash(balance, currency)}</div>
        <div class="muted" style="margin-top:6px">
          ${r.row_count || 0} 笔 / 流入 ${money(r.inflow, currency)} / 流出 ${money(r.outflow, currency)} / 净流 ${signedMoney(r.net_flow, currency)}
        </div>
        <div class="muted">余额日期: ${r.latest_balance_date || "-"}</div>
        ${rawBalanceNote(r)}
      </div>
    `;
  }).join("");
}

function renderRelationships() {
  const el = document.getElementById("relationshipList");
  const entities = config?.account_entities || {};
  const relationships = config?.account_relationships || [];
  if (!relationships.length) {
    el.innerHTML = '<div class="muted">暂无账户关系配置</div>';
    return;
  }
  el.innerHTML = relationships.map((r) => {
    const from = entities[r.from_account] || {};
    const to = entities[r.to_account] || {};
    return `
      <div class="metricPanel">
        <div class="label">${relationTypeLabel(r.relation_type)}</div>
        <div style="font-weight:800;margin-top:4px">${r.label || r.code}</div>
        <div class="muted" style="margin-top:6px">${from.label || r.from_account} → ${to.label || r.to_account}</div>
        <div class="muted">${from.role || "-"} / ${to.role || "-"}</div>
      </div>
    `;
  }).join("");
}

function renderTransferMatches(data) {
  const summaryEl = document.getElementById("transferMatchSummary");
  const rowsEl = document.getElementById("transferMatchRows");
  const summary = data?.summary || [];
  summaryEl.innerHTML = summary.length ? summary.map((r) => `
    <div class="metricPanel">
      <div class="label">${matchTypeLabel(r.match_type)}</div>
      <div class="value">${r.match_count || 0} 笔</div>
      <div class="muted" style="margin-top:6px">合计 ${money(r.total_amount, r.currency || "CNY")}</div>
    </div>
  `).join("") : '<div class="muted">暂无匹配结果</div>';

  const matches = data?.matches || [];
  rowsEl.innerHTML = matches.map((r) => {
    const source = `${r.source_date || "-"} ${compactText(r.source_summary)} / ${compactText(r.source_description)}`;
    const target = r.target_row_id
      ? `${r.target_date || "-"} ${compactText(r.target_summary)} / ${compactText(r.target_description)}`
      : "无平台侧成对流水";
    return `
      <tr>
        <td>${r.match_date || "-"}</td>
        <td>${matchTypeLabel(r.match_type)}</td>
        <td>${r.from_account_code || "-"} → ${r.to_account_code || "-"}</td>
        <td class="num">${money(r.amount, r.currency || "CNY")}</td>
        <td class="num">${confidence(r.confidence)}</td>
        <td>${r.note || "-"}</td>
        <td>${source}</td>
        <td>${target}</td>
      </tr>
    `;
  }).join("") || '<tr><td colspan="8" class="muted" style="text-align:center;padding:20px">暂无匹配结果</td></tr>';
}

function renderUnmatchedRows(data) {
  const summaryEl = document.getElementById("unmatchedSummary");
  const rowsEl = document.getElementById("unmatchedRows");
  if (!summaryEl || !rowsEl) return;

  const summary = data?.summary || [];
  summaryEl.innerHTML = summary.length ? summary.map((r) => `
    <div class="metricPanel">
      <div class="label">${issueTypeLabel(r.issue_type)}</div>
      <div class="value">${r.count || 0} 笔</div>
      <div class="muted" style="margin-top:6px">${esc(r.account_code || "-")} / ${money(r.amount_abs, r.currency || "CNY")}</div>
    </div>
  `).join("") : '<div class="muted">暂无待确认流水</div>';

  const rows = data?.rows || [];
  rowsEl.innerHTML = rows.map((r) => {
    const n = Number(r.amount || 0);
    const color = n >= 0 ? "var(--good)" : "var(--bad)";
    const category = `${r.category || "未分类"}${r.subcategory ? " / " + r.subcategory : ""}`;
    const relation = r.relation_code
      ? `${matchTypeLabel(r.match_type)} · ${r.from_account_code || "-"} → ${r.to_account_code || "-"}`
      : "";
    return `
      <tr>
        <td>${r.transaction_date || "-"}</td>
        <td>${issueTypeLabel(r.issue_type)}</td>
        <td>${esc(r.account_code || "-")}</td>
        <td class="num" style="color:${color}">${signedMoney(r.amount, r.currency || "CNY")}</td>
        <td>${esc(category)}</td>
        <td>${esc(compactText(`${r.summary || ""} ${r.description || ""}`, 120))}</td>
        <td>
          <div>${esc(r.issue_note || "-")}</div>
          ${relation ? `<div class="muted">${esc(relation)}</div>` : ""}
          ${r.counterparty ? `<div class="muted">${esc(r.counterparty)}</div>` : ""}
        </td>
      </tr>
    `;
  }).join("") || '<tr><td colspan="7" class="muted" style="text-align:center;padding:20px">暂无待确认流水</td></tr>';
}

function renderRows(statements) {
  const allRows = [];
  (statements || []).forEach((s) => {
    (s.rows || []).forEach((r) => allRows.push({ ...r, account_code: s.account_code, profile_code: s.profile_code }));
  });
  allRows.sort((a, b) => String(b.transaction_date).localeCompare(String(a.transaction_date)));
  const tbody = document.getElementById("previewRows");
  document.getElementById("rowMeta").textContent = `${allRows.length} 条，显示前 200 条`;
  tbody.innerHTML = allRows.slice(0, 200).map((r) => {
    const n = Number(r.amount || 0);
    const color = n >= 0 ? "var(--good)" : "var(--bad)";
    return `
      <tr>
        <td>${r.transaction_date || "-"}</td>
        <td>${profileLabel(r.profile_code)}</td>
        <td>${r.category || "未分类"}${r.subcategory ? " / " + r.subcategory : ""}</td>
        <td>${r.summary || "-"}</td>
        <td>${r.description || "-"}</td>
        <td class="num" style="color:${color}">${signedMoney(r.amount, r.currency || "CNY")}</td>
        <td class="num">${r.balance == null ? "—" : money(r.balance, r.currency || "CNY")}</td>
      </tr>
    `;
  }).join("") || '<tr><td colspan="7" class="muted" style="text-align:center;padding:20px">暂无流水</td></tr>';
}

async function preview() {
  clearError();
  setMsg("解析 PDF 中...");
  latestPreview = await jpost("/api/bank-reconcile/preview", buildRequest());
  renderSummary(latestPreview.summary);
  renderRows(latestPreview.statements);
  setMsg("预览完成。当前未写入 PAMS 主收支/持仓。");
}

async function doImport() {
  clearError();
  setMsg("导入暂存台账中...");
  const resp = await jpost("/api/bank-reconcile/import", buildRequest());
  renderSummary(resp.summary);
  await loadImports();
  await loadAccountOverview();
  setMsg(`导入完成：新增 ${resp.inserted_rows || 0} 条流水。`);
}

async function loadTransferMatches() {
  const resp = await jget("/api/bank-reconcile/transfer-matches");
  renderTransferMatches(resp);
}

async function loadUnmatchedRows() {
  const since = document.getElementById("since")?.value || config?.defaults?.since || "2026-01-01";
  const resp = await jget(`/api/bank-reconcile/unmatched-rows?since=${encodeURIComponent(since)}&limit=500`);
  renderUnmatchedRows(resp);
}

async function runTransferMatching() {
  clearError();
  setMsg("正在匹配账户间流水...");
  const resp = await jpost("/api/bank-reconcile/transfer-matches/run", {});
  renderTransferMatches(resp);
  await loadUnmatchedRows();
  await loadImports();
  await loadAccountOverview();
  setMsg(`匹配完成：${resp.matched || 0} 条关系。内部划转不会作为生活消费统计；支付通道映射用于去重。`);
}

async function loadImports() {
  const resp = await jget("/api/bank-reconcile/imports");
  const tbody = document.getElementById("importRows");
  tbody.innerHTML = (resp.imports || []).map((r) => `
    <tr>
      <td>${r.imported_at || "-"}</td>
      <td>${profileLabel(r.profile_code)}</td>
      <td>${r.account_code || "-"}</td>
      <td>${(r.source_file || "").split(/[\\/]/).pop()}</td>
      <td class="num">${r.row_count || 0}</td>
      <td class="num">${r.statement_balance == null ? "—" : money(r.statement_balance, profileCurrency(r.profile_code))}</td>
    </tr>
  `).join("") || '<tr><td colspan="6" class="muted" style="text-align:center;padding:20px">暂无导入记录</td></tr>';
  if (!latestPreview) renderSummary(resp.summary || []);
}

async function main() {
  setActiveNav();
  initAccountTabs();
  initAccountReviewActions();
  try {
    config = await jget("/api/bank-reconcile/config");
    await loadAccountOverview();
    document.getElementById("since").value = config?.defaults?.since || "2026-01-01";
    renderProfiles();
    renderRelationships();
    await loadTransferMatches();
    await loadUnmatchedRows();
    await loadImports();
    document.getElementById("btnPreview").addEventListener("click", () => preview().catch((e) => showError(e.message || String(e))));
    document.getElementById("btnImport").addEventListener("click", () => doImport().catch((e) => showError(e.message || String(e))));
    document.getElementById("btnMatchTransfers").addEventListener("click", () => runTransferMatching().catch((e) => showError(e.message || String(e))));
  } catch (e) {
    showError(e.message || String(e));
  }
}

main();
