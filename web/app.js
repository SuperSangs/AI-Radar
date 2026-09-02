const state = {
  range: "24h",
  region: "all",
  type: "all",
  query: "",
  refreshing: false,
  pollTimer: null,
  lastUpdatedAt: null,
};

const itemsByRange = new Map();
let activeItemsRequest = null;
let activeItemsRange = null;
let itemsLoadId = 0;

const elements = {
  list: document.getElementById("item-list"),
  empty: document.getElementById("empty-state"),
  emptyTitle: document.getElementById("empty-title"),
  emptyCopy: document.getElementById("empty-copy"),
  resultCount: document.getElementById("result-count"),
  todayCount: document.getElementById("today-count"),
  sourceCount: document.getElementById("source-count"),
  healthyCount: document.getElementById("healthy-count"),
  sourceList: document.getElementById("source-list"),
  sourceSummary: document.getElementById("source-summary"),
  refresh: document.getElementById("refresh-button"),
  updated: document.getElementById("updated-label"),
  liveDot: document.getElementById("live-dot"),
  search: document.getElementById("search-input"),
  region: document.getElementById("region-filter"),
  dailyBrief: document.getElementById("daily-brief"),
  briefTitle: document.getElementById("daily-brief-title"),
  briefOverview: document.getElementById("brief-overview"),
  briefThemes: document.getElementById("brief-themes"),
  briefDetails: document.getElementById("brief-details"),
  briefSignals: document.getElementById("brief-signals"),
  briefIdeas: document.getElementById("brief-ideas"),
  briefMeta: document.getElementById("brief-meta"),
  briefRefresh: document.getElementById("brief-refresh"),
  briefToggle: document.getElementById("brief-toggle"),
};

const typeNames = {
  project: "项目",
  model: "模型",
  paper: "论文",
  news: "资讯",
  discussion: "讨论",
};

function relativeTime(value) {
  if (!value) return "时间未知";
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function sourceLink(source) {
  const link = el("a", "source-chip", source.name);
  link.href = source.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function friendlySourceError(error) {
  const message = String(error || "").toLowerCase();
  if (message.includes("timed out") || message.includes("timeout")) return "连接超时，下次刷新会重试";
  if (message.includes("reset by peer")) return "连接被站点中断，下次刷新会重试";
  if (message.includes("402")) return "API 余额不足或计费未启用";
  if (message.includes("429")) return "接口限额已达上限，稍后重试";
  if (message.includes("401") || message.includes("403")) return "站点暂时拒绝访问";
  if (message.includes("404")) return "订阅地址已失效";
  if (message.includes("parseerror")) return "订阅格式暂时无法解析";
  return "采集失败，下次刷新会重试";
}

function renderDailyBrief(data) {
  elements.dailyBrief.classList.remove("loading", "error");
  elements.dailyBrief.classList.remove("expanded");
  elements.dailyBrief.setAttribute("aria-busy", "false");
  elements.briefTitle.textContent = data.headline;
  elements.briefOverview.textContent = data.overview;
  elements.briefThemes.replaceChildren();
  elements.briefSignals.replaceChildren();
  elements.briefIdeas.replaceChildren();

  for (const theme of data.themes || []) {
    const card = el("article", "brief-theme");
    card.append(el("h3", "", theme.name), el("p", "", theme.summary));
    elements.briefThemes.append(card);
  }
  for (const signal of data.key_signals || []) elements.briefSignals.append(el("li", "", signal));
  for (const idea of data.content_ideas || []) elements.briefIdeas.append(el("li", "", idea));

  elements.briefThemes.hidden = !(data.themes || []).length;
  const hasDetails = Boolean((data.key_signals || []).length || (data.content_ideas || []).length);
  elements.briefDetails.hidden = true;
  elements.briefToggle.hidden = !hasDetails && !(data.themes || []).length;
  elements.briefToggle.setAttribute("aria-expanded", "false");
  elements.briefToggle.textContent = "展开完整分析";
  const staleLabel = data.stale ? " · 当前为上次成功结果" : "";
  elements.briefMeta.textContent = `DeepSeek · 基于 ${data.item_count || 0} 条已采集内容 · ${relativeTime(data.generated_at)}生成${staleLabel}`;
}

function renderDailyBriefError() {
  elements.dailyBrief.classList.remove("loading");
  elements.dailyBrief.classList.add("error");
  elements.dailyBrief.setAttribute("aria-busy", "false");
  elements.briefTitle.textContent = "今日总结暂时不可用";
  elements.briefOverview.textContent = "已采集的情报榜单仍可正常使用；DeepSeek 接口恢复后会再次生成。";
  elements.briefThemes.hidden = true;
  elements.briefDetails.hidden = true;
  elements.briefToggle.hidden = true;
  elements.briefMeta.textContent = "你可以稍后点击“重新总结”。";
}

async function loadDailyBrief({ force = false } = {}) {
  elements.briefRefresh.disabled = true;
  if (force) {
    elements.dailyBrief.classList.add("loading");
    elements.dailyBrief.setAttribute("aria-busy", "true");
    elements.briefMeta.textContent = "正在使用最新采集内容重新生成…";
  }
  try {
    const response = await fetch("/api/daily-summary", { method: force ? "POST" : "GET" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderDailyBrief(await response.json());
  } catch (error) {
    renderDailyBriefError();
  } finally {
    elements.briefRefresh.disabled = false;
  }
}

function renderItems(items) {
  const fragment = document.createDocumentFragment();
  elements.list.setAttribute("aria-busy", "false");
  elements.resultCount.textContent = `${items.length} 条结果`;
  elements.empty.hidden = items.length > 0;
  elements.list.hidden = items.length === 0;

  for (const item of items) {
    const row = el("li", "rank-item");
    row.append(el("span", "rank-number"));

    const main = el("article", "item-main");
    const meta = el("div", "item-meta");
    meta.append(el("span", `type-badge ${item.content_type}`, typeNames[item.content_type] || "资讯"));
    meta.append(el("span", "", item.region === "china" ? "国内" : "海外"));
    const discoveryDelay = new Date(item.discovered_at).getTime() - new Date(item.published_at).getTime();
    meta.append(el("span", "", discoveryDelay > 12 * 3600 * 1000 ? `${relativeTime(item.effective_at)}发现` : relativeTime(item.published_at)));
    if (item.engagement > 0) meta.append(el("span", "", `${item.engagement.toLocaleString()} 热度`));

    const title = el("h3", "item-title");
    const titleLink = el("a", "", item.title);
    titleLink.href = item.url;
    titleLink.target = "_blank";
    titleLink.rel = "noopener noreferrer";
    title.append(titleLink);
    main.append(meta, title);
    if (item.summary) main.append(el("p", "item-summary", item.summary));

    const footer = el("div", "item-footer");
    for (const source of item.sources.slice(0, 3)) footer.append(sourceLink(source));
    if (item.source_count > 3) footer.append(el("span", "tag", `另 ${item.source_count - 3} 个来源`));
    for (const tag of item.tags.slice(0, 3)) {
      if (tag && String(tag).length < 26) footer.append(el("span", "tag", String(tag)));
    }
    main.append(footer);

    const score = el("div", "item-score");
    score.append(el("strong", "", String(item.score)), el("span", "", "综合分"));
    row.append(main, score);
    fragment.append(row);
  }
  elements.list.replaceChildren(fragment);
}

function renderSources(sources) {
  elements.sourceList.replaceChildren();
  const healthy = sources.filter((source) => source.last_success_at && !source.last_error).length;
  const errors = sources.filter((source) => source.last_error).length;
  elements.sourceCount.textContent = String(sources.length);
  elements.healthyCount.textContent = String(healthy);
  elements.sourceSummary.replaceChildren();

  const healthyStat = el("div");
  healthyStat.append(el("strong", "", String(healthy)), el("span", "", "正常"));
  const errorStat = el("div");
  errorStat.append(el("strong", "", String(errors)), el("span", "", "待恢复"));
  elements.sourceSummary.append(healthyStat, errorStat);

  const ordered = [...sources].sort((a, b) => {
    const aRank = a.last_error ? 0 : a.last_success_at ? 2 : 1;
    const bRank = b.last_error ? 0 : b.last_success_at ? 2 : 1;
    return aRank - bRank || a.name.localeCompare(b.name, "zh-CN");
  });
  for (const source of ordered) {
    const row = el("li");
    const top = el("div", "source-row");
    const link = el("a", "source-name", source.name);
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const statusClass = source.last_error ? "error" : source.last_success_at ? "ok" : "";
    const statusText = source.last_error ? "异常" : source.last_success_at ? `${source.last_item_count} 条` : "等待";
    top.append(link, el("span", `source-state ${statusClass}`, statusText));
    row.append(top);
    if (source.last_error) {
      row.append(el("div", "source-detail", friendlySourceError(source.last_error)));
    } else if (source.key === "x-ai" && source.resource_day) {
      const estimatedCost = (Number(source.resource_count || 0) * 0.005).toFixed(3);
      row.append(el("div", "source-detail", `本计费日读取 ${source.resource_count || 0} 条 · 约 $${estimatedCost}`));
    }
    elements.sourceList.append(row);
  }
}

function filterItems(items) {
  const query = state.query.toLocaleLowerCase();
  return items.filter((item) => {
    if (state.region !== "all" && item.region !== state.region) return false;
    if (state.type !== "all" && item.content_type !== state.type) return false;
    if (!query) return true;
    const searchable = [
      item.title,
      item.summary,
      item.author,
      ...(item.tags || []),
      ...(item.sources || []).map((source) => source.name),
    ].join(" ").toLocaleLowerCase();
    return searchable.includes(query);
  });
}

function renderCurrentItems() {
  const cached = itemsByRange.get(state.range);
  if (!cached) {
    if (!activeItemsRequest || activeItemsRange !== state.range) loadItems();
    return;
  }
  const items = filterItems(cached);
  renderItems(items);
  if (!items.length) {
    elements.emptyTitle.textContent = state.query ? "没有匹配的内容" : "这个时间段还没有内容";
    elements.emptyCopy.textContent = state.query ? "换一个关键词，或放宽时间和类型筛选。" : "点击刷新，从各个信息源获取最新内容。";
  }
}

async function loadItems({ force = false } = {}) {
  const range = state.range;
  if (!force && itemsByRange.has(range)) {
    activeItemsRequest?.abort();
    activeItemsRequest = null;
    activeItemsRange = null;
    renderCurrentItems();
    return;
  }

  activeItemsRequest?.abort();
  const controller = new AbortController();
  activeItemsRequest = controller;
  activeItemsRange = range;
  const loadId = ++itemsLoadId;
  elements.list.setAttribute("aria-busy", "true");
  elements.resultCount.textContent = "加载中";
  const params = new URLSearchParams({
    range,
    region: "all",
    type: "all",
    q: "",
    limit: "500",
  });
  try {
    const response = await fetch(`/api/items?${params}`, { signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (loadId !== itemsLoadId) return;
    itemsByRange.set(range, data.items);
    if (state.range === range) renderCurrentItems();
  } catch (error) {
    if (error.name === "AbortError") return;
    elements.list.hidden = true;
    elements.empty.hidden = false;
    elements.emptyTitle.textContent = "榜单暂时无法加载";
    elements.emptyCopy.textContent = "服务可能正在启动，请稍后再试。";
    elements.resultCount.textContent = "连接失败";
  } finally {
    if (activeItemsRequest === controller) {
      activeItemsRequest = null;
      activeItemsRange = null;
    }
  }
}

async function loadSummary() {
  try {
    const [summaryResponse, sourcesResponse] = await Promise.all([
      fetch("/api/summary"),
      fetch("/api/sources"),
    ]);
    if (!summaryResponse.ok || !sourcesResponse.ok) throw new Error("summary failed");
    const summary = await summaryResponse.json();
    const sources = await sourcesResponse.json();
    elements.todayCount.textContent = String(summary.today || 0);
    renderSources(sources.sources);
    updateRefreshState(summary.refreshing, summary.last_run?.finished_at || summary.updated_at, sources.sources);
  } catch (error) {
    elements.updated.textContent = "服务未连接";
    elements.liveDot.className = "status-dot error";
  }
}

function updateRefreshState(refreshing, updatedAt, sources) {
  const wasRefreshing = state.refreshing;
  const dataChanged = Boolean(state.lastUpdatedAt && updatedAt && state.lastUpdatedAt !== updatedAt);
  state.refreshing = Boolean(refreshing);
  if (updatedAt) state.lastUpdatedAt = updatedAt;
  elements.refresh.disabled = state.refreshing;
  elements.liveDot.className = `status-dot ${state.refreshing ? "busy" : "ok"}`;
  elements.updated.textContent = state.refreshing
    ? "正在采集最新内容"
    : updatedAt
      ? `${relativeTime(updatedAt)}更新`
      : "等待首次采集";
  if ((wasRefreshing && !state.refreshing) || (dataChanged && !state.refreshing)) {
    itemsByRange.clear();
    loadItems({ force: true });
    loadDailyBrief();
  }
  if (state.refreshing && !state.pollTimer) {
    state.pollTimer = window.setInterval(loadSummary, 4000);
  } else if (!state.refreshing && state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  if (!state.refreshing && sources.some((source) => source.last_error) && !sources.some((source) => source.last_success_at)) {
    elements.liveDot.className = "status-dot error";
  }
}

async function refresh() {
  elements.refresh.disabled = true;
  try {
    await fetch("/api/refresh", { method: "POST" });
    state.refreshing = true;
    elements.liveDot.className = "status-dot busy";
    elements.updated.textContent = "正在采集最新内容";
    if (!state.pollTimer) state.pollTimer = window.setInterval(loadSummary, 4000);
  } catch (error) {
    elements.refresh.disabled = false;
    elements.updated.textContent = "刷新请求失败";
    elements.liveDot.className = "status-dot error";
  }
}

function bindPressedGroup(id, key) {
  document.getElementById(id).addEventListener("click", (event) => {
    const button = event.target.closest("button[data-value]");
    if (!button) return;
    for (const peer of button.parentElement.querySelectorAll("button")) peer.setAttribute("aria-pressed", String(peer === button));
    state[key] = button.dataset.value;
    if (key === "range") loadItems();
    else renderCurrentItems();
  });
}

let searchTimer;
elements.search.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    state.query = elements.search.value.trim();
    renderCurrentItems();
  }, 250);
});
elements.region.addEventListener("change", () => {
  state.region = elements.region.value;
  renderCurrentItems();
});
elements.refresh.addEventListener("click", refresh);
elements.briefRefresh.addEventListener("click", () => loadDailyBrief({ force: true }));
elements.briefToggle.addEventListener("click", () => {
  const expanded = elements.dailyBrief.classList.toggle("expanded");
  elements.briefDetails.hidden = !expanded;
  elements.briefToggle.setAttribute("aria-expanded", String(expanded));
  elements.briefToggle.textContent = expanded ? "收起完整分析" : "展开完整分析";
});
document.getElementById("source-toggle").addEventListener("click", (event) => {
  const aside = document.querySelector(".sources");
  const expanded = aside.classList.toggle("expanded");
  event.currentTarget.setAttribute("aria-expanded", String(expanded));
  event.currentTarget.textContent = expanded ? "收起" : "展开";
});

bindPressedGroup("range-filter", "range");
bindPressedGroup("type-filter", "type");
document.getElementById("date-label").textContent = new Intl.DateTimeFormat("zh-CN", {
  month: "long",
  day: "numeric",
  weekday: "long",
}).format(new Date());

loadItems();
loadSummary();
loadDailyBrief();
window.setInterval(loadSummary, 60_000);
