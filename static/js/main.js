const summaryKeys = {
  totalJobs: document.getElementById('totalJobs'),
  avgSalary: document.getElementById('avgSalary'),
  cityCount: document.getElementById('cityCount'),
  topSkill: document.getElementById('topSkill'),
};

const cityFilter = document.getElementById('cityFilter');
const educationFilter = document.getElementById('educationFilter');
const experienceFilter = document.getElementById('experienceFilter');
const categoryFilter = document.getElementById('categoryFilter');
const keywordInput = document.getElementById('keywordInput');
const resultHint = document.getElementById('resultHint');
const applyFilterBtn = document.getElementById('applyFilterBtn');
const resetFilterBtn = document.getElementById('resetFilterBtn');
const dataFileInput = document.getElementById('dataFileInput');
const uploadDataBtn = document.getElementById('uploadDataBtn');
const resetDataBtn = document.getElementById('resetDataBtn');
const detailDialog = document.getElementById('detailDialog');
const detailContent = document.getElementById('detailContent');
const sortSelect = document.getElementById('sortSelect');
const pageSizeSelect = document.getElementById('pageSizeSelect');
const tableHint = document.getElementById('tableHint');
const pageInfo = document.getElementById('pageInfo');
const prevPageBtn = document.getElementById('prevPageBtn');
const nextPageBtn = document.getElementById('nextPageBtn');
const charts = new Map();

// 岗位表格的分页/排序状态
const tableState = { page: 0, pageSize: 20, sort: 'salary', order: 'desc', total: 0 };

function formatYuan(value) {
  return `¥${Number(value || 0).toLocaleString()}元`;
}

function formatSalaryRange(low, high) {
  if (low == null && high == null) return '未知';
  return `${Number(low || 0).toLocaleString()}-${Number(high || 0).toLocaleString()}元`;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

function setOptions(select, values, placeholder) {
  select.replaceChildren();
  const first = document.createElement('option');
  first.value = '';
  first.textContent = placeholder;
  select.append(first);
  values.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
}

async function loadFilterOptions() {
  const options = await fetchJson('/api/options');
  setOptions(cityFilter, options.city || [], '全部城市');
  setOptions(educationFilter, options.education || [], '全部学历');
  setOptions(experienceFilter, options.experience || [], '全部经验');
  setOptions(categoryFilter, options.category || [], '全部类别');
}

async function loadDataSource() {
  const data = await fetchJson('/api/data-source');
  document.getElementById('dataSourceHint').textContent = `${data.source}（${data.count} 条）`;
}

async function uploadData() {
  const file = dataFileInput.files[0];
  if (!file) {
    document.getElementById('dataSourceHint').textContent = '请先选择文件';
    return;
  }
  const formData = new FormData();
  formData.append('file', file);
  uploadDataBtn.disabled = true;
  try {
    const response = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '上传失败');
    await loadDataSource();
    await loadFilterOptions();
    await loadDashboard();
    dataFileInput.value = '';
  } catch (error) {
    document.getElementById('dataSourceHint').textContent = error.message;
  } finally {
    uploadDataBtn.disabled = false;
  }
}

async function resetData() {
  await fetchJson('/api/reset-data', { method: 'POST' });
  await loadDataSource();
  await loadFilterOptions();
  await loadDashboard();
}

function buildQueryString() {
  const params = new URLSearchParams();
  [[cityFilter, 'city'], [educationFilter, 'education'],
    [experienceFilter, 'experience'], [categoryFilter, 'category']]
    .forEach(([control, name]) => {
      if (control.value) params.set(name, control.value);
    });
  if (keywordInput.value.trim()) params.set('keyword', keywordInput.value.trim());
  params.set('limit', '10');
  return `?${params.toString()}`;
}

/** 表格专用查询：带排序与分页（看板统计仍用 buildQueryString）。 */
function buildTableQueryString() {
  const params = new URLSearchParams(buildQueryString());
  params.delete('limit');
  if (tableState.sort) params.set('sort', tableState.sort);
  if (tableState.order) params.set('order', tableState.order);
  params.set('limit', String(tableState.pageSize));
  params.set('offset', String(tableState.page * tableState.pageSize));
  return `?${params.toString()}`;
}

function updateExportLinks() {
  const query = buildQueryString().replace('limit=10', 'limit=100');
  document.getElementById('csvExport').href = `/export/csv${query}`;
  document.getElementById('excelExport').href = `/export/excel${query}`;
}

function renderCompanyList(data) {
  const container = document.getElementById('companyList');
  container.replaceChildren();
  if (!data || !data.length) {
    const empty = document.createElement('div');
    empty.className = 'company-item';
    empty.textContent = '暂无数据';
    container.append(empty);
    return;
  }
  data.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'company-item';
    const name = document.createElement('strong');
    name.textContent = item.name;
    const count = document.createElement('span');
    count.textContent = `${item.value}个岗位`;
    row.append(name, count);
    container.append(row);
  });
}

function chartFor(id) {
  if (!charts.has(id)) {
    charts.set(id, echarts.init(document.getElementById(id)));
  }
  return charts.get(id);
}

// 只注册一个 resize 监听：以前每个图表各注册一次，7 个图表就有 7 个监听器。
window.addEventListener('resize', () => {
  charts.forEach((chart) => chart.resize());
});

function safeChartName(value) {
  return String(value || '').replace(/[<>&"']/g, (char) => ({
    '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

// 多序列图表的配色（趋势线、饼图共用）
const PALETTE = ['#3d6f9f', '#b7791f', '#2f855a', '#9b2c2c', '#6b46c1',
  '#2c7a7b', '#718096', '#b83280', '#4a5568', '#dd6b20'];

/** 岗位个数用「1,234」这种带千分位的写法，避免大数字难读。 */
function formatCount(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

/**
 * 柱状图。改动点：横向柱条上直接标数值、tooltip 带千分位与占比。
 */
function renderBarChart(id, data, horizontal = false, color = PALETTE[0]) {
  const chart = chartFor(id);
  const items = data || [];
  const names = items.map((item) => safeChartName(item.name));
  const values = items.map((item) => Number(item.value) || 0);
  const total = values.reduce((sum, value) => sum + value, 0);
  const share = (value) => (total ? `（占 ${(value / total * 100).toFixed(1)}%）` : '');

  chart.setOption({
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const list = Array.isArray(params) ? params : [params];
        return list.map((item) => {
          const value = Number(item.value) || 0;
          return `${safeChartName(item.name)}：${formatCount(value)} 个岗位${share(value)}`;
        }).join('<br/>');
      },
    },
    grid: { left: horizontal ? 70 : 45, right: 45, top: 20, bottom: 45, containLabel: true },
    xAxis: horizontal
      ? { type: 'value', axisLabel: { color: '#526579' } }
      : { type: 'category', data: names,
          axisLabel: { color: '#526579', rotate: names.length > 7 ? 25 : 0 } },
    yAxis: horizontal
      ? { type: 'category', data: names, axisLabel: { color: '#526579' } }
      : { type: 'value', axisLabel: { color: '#526579' } },
    series: [{
      type: 'bar',
      data: values,
      barMaxWidth: 26,
      itemStyle: { color, borderRadius: 5 },
      label: {
        show: true,
        position: horizontal ? 'right' : 'top',
        color: '#526579',
        fontSize: 11,
        formatter: (params) => formatCount(params.value),
      },
    }],
  }, true);
}

/**
 * 饼图。改动点：标签直接显示「名称 数量（占比）」，比只给名称好读。
 */
function renderPieChart(id, data) {
  const chart = chartFor(id);
  const items = data || [];
  const safeData = items.map((item, index) => ({
    name: safeChartName(item.name),
    value: Number(item.value) || 0,
    itemStyle: { color: PALETTE[index % PALETTE.length] },
  }));
  chart.setOption({
    animation: false,
    color: PALETTE,
    tooltip: {
      trigger: 'item',
      formatter: (params) => `${params.name}：${formatCount(params.value)} 个岗位`
        + `（${params.percent}%）`,
    },
    legend: { bottom: 0, textStyle: { color: '#526579' } },
    series: [{
      type: 'pie',
      radius: ['42%', '72%'],
      center: ['50%', '42%'],
      data: safeData,
      label: {
        color: '#243447',
        formatter: (params) => `${params.name}\n${formatCount(params.value)}（${params.percent}%）`,
      },
      labelLine: { lineStyle: { color: '#c3cdd8' } },
    }],
  }, true);
}

/**
 * 采集趋势折线图（多序列）。数据来自 /api/trends，即 SQLite 的 postings 表。
 * 数据库不存在时由调用方给出提示并隐藏卡片，不在这里报错。
 * totals 是「当日抓到的岗位总数」，作为参考线一起画，避免只看到相对高低。
 */
function renderLineChart(id, dates, series, totals) {
  const chart = chartFor(id);
  const labels = (dates || []).map((day) => String(day).slice(5));
  const lines = (series || []).map((item, index) => ({
    name: safeChartName(item.name),
    type: 'line',
    smooth: true,
    showSymbol: true,
    symbolSize: 6,
    lineStyle: { width: 2, color: PALETTE[index % PALETTE.length] },
    itemStyle: { color: PALETTE[index % PALETTE.length] },
    data: (item.data || []).map((value) => Number(value) || 0),
  }));
  if (Array.isArray(totals) && totals.length) {
    lines.push({
      name: '当日岗位总数',
      type: 'line',
      smooth: true,
      showSymbol: false,
      lineStyle: { width: 1.5, type: 'dashed', color: '#a0aec0' },
      itemStyle: { color: '#a0aec0' },
      data: totals.map((value) => Number(value) || 0),
    });
  }

  chart.setOption({
    animation: false,
    color: PALETTE,
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const list = Array.isArray(params) ? params : [params];
        const head = list.length ? `${list[0].axisValue}` : '';
        return [head, ...list.map((item) =>
          `${item.marker}${item.seriesName}：${formatCount(item.value)} 条`)].join('<br/>');
      },
    },
    legend: { top: 0, textStyle: { color: '#526579' } },
    grid: { left: 55, right: 25, top: 45, bottom: 40, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: labels,
      axisLabel: { color: '#526579' },
    },
    yAxis: {
      type: 'value',
      name: '条数',
      nameTextStyle: { color: '#93a1b0', fontSize: 11 },
      axisLabel: { color: '#526579' },
      splitLine: { lineStyle: { color: '#eef2f6' } },
    },
    series: lines,
  }, true);
}

/** 加载岗位表格当前页（分页 + 排序），并更新翻页控件。 */
async function loadJobs() {
  try {
    const data = await fetchJson(`/api/jobs-page${buildTableQueryString()}`);
    tableState.total = data.total || 0;
    renderJobTable(data.items);
    const pages = Math.max(1, Math.ceil(tableState.total / tableState.pageSize));
    tableState.page = Math.min(tableState.page, pages - 1);
    const from = tableState.total ? tableState.page * tableState.pageSize + 1 : 0;
    const to = Math.min(tableState.total, (tableState.page + 1) * tableState.pageSize);
    if (pageInfo) pageInfo.textContent = `第 ${tableState.page + 1} / ${pages} 页`;
    if (tableHint) tableHint.textContent = tableState.total ? `显示第 ${from}-${to} 条，共 ${tableState.total} 条` : '没有符合条件的岗位';
    if (prevPageBtn) prevPageBtn.disabled = tableState.page <= 0;
    if (nextPageBtn) nextPageBtn.disabled = tableState.page >= pages - 1;
  } catch (error) {
    resultHint.textContent = error.message;
  }
}

/** 筛选条件变化时回到第一页，否则会停在越界的页码上。 */
function reloadJobsFromFirstPage() {
  tableState.page = 0;
  return loadJobs();
}

function renderJobTable(data) {
  const tbody = document.getElementById('jobTableBody');
  tbody.replaceChildren();
  if (!data || !data.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 9;
    cell.className = 'empty-cell';
    cell.textContent = '没有符合条件的岗位';
    row.append(cell);
    tbody.append(row);
    return;
  }
  data.forEach((item) => {
    const row = document.createElement('tr');
    [
      item.job_name,
      item.company,
      item.city,
      formatSalaryRange(item.salary_low, item.salary_high),
      item.education,
      item.experience,
      item.category,
      item.skills,
    ].forEach((value) => {
      const cell = document.createElement('td');
      cell.textContent = value == null ? '未知' : value;
      row.append(cell);
    });
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'detail-btn';
    button.textContent = '详情';
    button.addEventListener('click', () => showDetail(item));
    actionCell.append(button);
    row.append(actionCell);
    tbody.append(row);
  });
}

/**
 * 打开岗位详情。
 * 优先用稳定标识 job_key 取详情 —— 行号式的 job_id 每次重新导入都会漂移。
 */
async function showDetail(item) {
  const key = item && item.job_key ? String(item.job_key) : '';
  const url = key
    ? `/api/jobs/by-key/${encodeURIComponent(key)}`
    : `/api/jobs/${encodeURIComponent(item.job_id)}`;
  try {
    const detail = await fetchJson(url);
    document.getElementById('detailTitle').textContent = detail.job_name || '岗位详情';
    detailContent.replaceChildren();
    const fields = [
      ['公司', detail.company], ['城市', detail.city],
      ['薪资', formatSalaryRange(detail.salary_low, detail.salary_high)],
      ['学历', detail.education], ['经验', detail.experience],
      ['行业/类别', detail.category], ['技能', detail.skills],
      ['职位描述', detail.description || '暂无职位描述'],
    ];
    fields.forEach(([label, value]) => {
      const row = document.createElement('p');
      const title = document.createElement('strong');
      title.textContent = `${label}：`;
      row.append(title, document.createTextNode(value == null ? '未知' : value));
      detailContent.append(row);
    });
    if (detail.job_link) {
      const row = document.createElement('p');
      const title = document.createElement('strong');
      title.textContent = '原始岗位：';
      const link = document.createElement('a');
      link.href = detail.job_link;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '在 BOSS 直聘打开';
      row.append(title, link);
      detailContent.append(row);
    }
    detailDialog.showModal();
  } catch (error) {
    resultHint.textContent = error.message;
  }
}

async function loadDashboard() {
  try {
    const data = await fetchJson(`/api/dashboard${buildQueryString()}`);
    const summary = data.summary || {};
    summaryKeys.totalJobs.textContent = summary.total_jobs || 0;
    summaryKeys.avgSalary.textContent = formatYuan(summary.avg_salary);
    summaryKeys.cityCount.textContent = summary.city_count || 0;
    summaryKeys.topSkill.textContent = summary.top_skill || '无';
    resultHint.textContent = `共 ${summary.total_jobs || 0} 条岗位记录 · 文本分析：${summary.text_analyzer || '规则分词降级'}`;
    renderCompanyList(data.top_companies);
    renderBarChart('salaryChart', data.salary, false);
    renderPieChart('educationChart', data.education);
    renderBarChart('cityChart', data.city, true);
    renderBarChart('experienceChart', data.experience, false);
    renderBarChart('categoryChart', data.category, true);
    renderBarChart('skillChart', data.skills, true);
    renderBarChart('keywordChart', data.keywords, true);
    // 表格改由 loadJobs() 单独取数（支持分页/排序），这里不再用看板里的前 10 条
    updateExportLinks();
  } catch (error) {
    resultHint.textContent = error.message;
    Object.values(summaryKeys).forEach((element) => { element.textContent = '—'; });
  }
}

/**
 * 加载采集趋势。与看板统计不同，它读的是 SQLite 快照表，
 * 因此不随筛选条件变化；不可用时隐藏卡片并说明原因。
 */
async function loadTrends() {
  const card = document.getElementById('trendCard');
  const hint = document.getElementById('trendHint');
  try {
    const data = await fetchJson('/api/trends');
    if (!data.available) {
      if (card) card.hidden = true;
      if (hint) hint.textContent = data.reason || '';
      return;
    }
    if (card) card.hidden = false;
    const span = data.dates.length ? `${data.dates[0]} ~ ${data.dates[data.dates.length - 1]}` : '';
    const totals = (data.totals || []).reduce((sum, value) => sum + value, 0);
    if (hint) {
      hint.textContent = `${span} · 共 ${totals} 条快照 · 数据库 ${data.coverage?.jobs || 0} 个岗位`;
    }
    renderLineChart('trendChart', data.dates, data.skills, data.totals);
  } catch (error) {
    if (card) card.hidden = true;
    if (hint) hint.textContent = `趋势不可用：${error.message}`;
  }
}

function refreshAll() {
  loadDashboard();
  reloadJobsFromFirstPage();
}

/** 输入防抖：搜索框边打字边搜，但不至于每个字符都打一次接口。 */
function debounce(fn, wait = 320) {
  let timer = null;
  return (...args) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, wait);
  };
}

const refreshAllDebounced = debounce(refreshAll);

applyFilterBtn.addEventListener('click', refreshAll);
keywordInput.addEventListener('input', refreshAllDebounced);
keywordInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') refreshAll();
  if (event.key === 'Escape') {
    keywordInput.value = '';
    refreshAll();
  }
});
resetFilterBtn.addEventListener('click', () => {
  [cityFilter, educationFilter, experienceFilter, categoryFilter].forEach((control) => { control.value = ''; });
  keywordInput.value = '';
  refreshAll();
});
// 下拉筛选即时生效，不需要再点一次「筛选」
[cityFilter, educationFilter, experienceFilter, categoryFilter].forEach((control) => {
  control?.addEventListener('change', refreshAll);
});
sortSelect?.addEventListener('change', () => {
  const [sort, order] = sortSelect.value.split(':');
  tableState.sort = sort;
  tableState.order = order;
  reloadJobsFromFirstPage();
});
pageSizeSelect?.addEventListener('change', () => {
  tableState.pageSize = Number(pageSizeSelect.value) || 20;
  reloadJobsFromFirstPage();
});
prevPageBtn?.addEventListener('click', () => {
  if (tableState.page > 0) {
    tableState.page -= 1;
    loadJobs();
  }
});
nextPageBtn?.addEventListener('click', () => {
  tableState.page += 1;
  loadJobs();
});
document.getElementById('closeDetailBtn').addEventListener('click', () => detailDialog.close());
uploadDataBtn.addEventListener('click', uploadData);
resetDataBtn.addEventListener('click', resetData);

function showBootError(message) {
  const banner = document.getElementById('bootError');
  if (!banner) return;
  banner.textContent = message;
  banner.hidden = false;
}

document.addEventListener('DOMContentLoaded', async () => {
  // 图表库缺失时给出明确提示，而不是让整页静默空白
  if (typeof echarts === 'undefined') {
    showBootError(
      '图表库 ECharts 未能加载：本地 static/vendor/echarts.min.js 不可用，'
      + 'CDN 回退也失败了。数据表格仍可正常使用；恢复图表请检查该文件是否存在。'
    );
  }
  try {
    await loadDataSource();
    await loadFilterOptions();
    await loadDashboard();
    await loadTrends();
    await loadJobs();
  } catch (error) {
    resultHint.textContent = error.message;
  }
});
