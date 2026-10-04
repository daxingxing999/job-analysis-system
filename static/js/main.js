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
const charts = new Map();

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

function renderBarChart(id, data, horizontal = false, color = '#4cc9f0') {
  const chart = chartFor(id);
  const names = (data || []).map((item) => safeChartName(item.name));
  const values = (data || []).map((item) => item.value);
  chart.setOption({
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: horizontal ? 70 : 45, right: 25, top: 20, bottom: 45, containLabel: true },
    xAxis: horizontal ? { type: 'value', axisLabel: { color: '#526579' } }
      : { type: 'category', data: names, axisLabel: { color: '#526579', rotate: names.length > 7 ? 25 : 0 } },
    yAxis: horizontal ? { type: 'category', data: names, axisLabel: { color: '#526579' } }
      : { type: 'value', axisLabel: { color: '#526579' } },
    series: [{ type: 'bar', data: values, barMaxWidth: 26, itemStyle: { color, borderRadius: 5 } }],
  }, true);
}

function renderPieChart(id, data) {
  const chart = chartFor(id);
  const safeData = (data || []).map((item) => ({
    name: safeChartName(item.name),
    value: Number(item.value) || 0,
  }));
  chart.setOption({
    animation: false,
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: '#526579' } },
    series: [{ type: 'pie', radius: ['42%', '72%'], center: ['50%', '42%'],
      data: safeData, label: { color: '#243447' } }],
  }, true);
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
    button.addEventListener('click', () => showDetail(item.job_id));
    actionCell.append(button);
    row.append(actionCell);
    tbody.append(row);
  });
}

async function showDetail(jobId) {
  try {
    const item = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    document.getElementById('detailTitle').textContent = item.job_name || '岗位详情';
    detailContent.replaceChildren();
    const fields = [
      ['公司', item.company], ['城市', item.city],
      ['薪资', formatSalaryRange(item.salary_low, item.salary_high)],
      ['学历', item.education], ['经验', item.experience],
      ['行业/类别', item.category], ['技能', item.skills],
      ['职位描述', item.description || '暂无职位描述'],
    ];
    fields.forEach(([label, value]) => {
      const row = document.createElement('p');
      const title = document.createElement('strong');
      title.textContent = `${label}：`;
      row.append(title, document.createTextNode(value == null ? '未知' : value));
      detailContent.append(row);
    });
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
    renderBarChart('salaryChart', data.salary, false, '#3d6f9f');
    renderPieChart('educationChart', data.education);
    renderBarChart('cityChart', data.city, true, '#4f7d9f');
    renderBarChart('experienceChart', data.experience, false, '#6b7280');
    renderBarChart('categoryChart', data.category, true, '#718096');
    renderBarChart('skillChart', data.skills, true, '#b7791f');
    renderBarChart('keywordChart', data.keywords, true, '#5b7794');
    renderJobTable(data.jobs);
    updateExportLinks();
  } catch (error) {
    resultHint.textContent = error.message;
    Object.values(summaryKeys).forEach((element) => { element.textContent = '—'; });
  }
}

applyFilterBtn.addEventListener('click', loadDashboard);
keywordInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadDashboard();
});
resetFilterBtn.addEventListener('click', () => {
  [cityFilter, educationFilter, experienceFilter, categoryFilter].forEach((control) => { control.value = ''; });
  keywordInput.value = '';
  loadDashboard();
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
  } catch (error) {
    resultHint.textContent = error.message;
  }
});
