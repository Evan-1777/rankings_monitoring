const state = {
  overview: null,
  view: "events",
  eventType: "all",
  eventCategory: "all",
  boardCategory: "",
  boardDataset: "",
  timer: null,
  refreshing: false,
  eventsRequest: 0,
  boardRequest: 0,
};

const EVENT_LABELS = {
  model_added: "新增模型",
  score_changed: "分数变更",
  dataset_added: "新榜单",
  model_removed: "移除",
};

const numberFormatter = new Intl.NumberFormat("zh-CN");
const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const dayFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "long",
  day: "numeric",
});

function $(selector, root = document) {
  return root.querySelector(selector);
}

function toDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatRelative(value) {
  const date = toDate(value);
  if (!date) return "—";
  const diff = Date.now() - date.getTime();
  if (diff < 0) return "刚刚";
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return dateTimeFormatter.format(date);
}

function formatTime(value) {
  const date = toDate(value);
  return date ? timeFormatter.format(date) : "—";
}

function formatDay(value) {
  const date = toDate(value);
  return date ? dayFormatter.format(date) : "";
}

function formatCount(value) {
  return numberFormatter.format(Number.isFinite(value) ? value : 0);
}

async function fetchJson(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { Accept: "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body.error || "";
    } catch {
      detail = "";
    }
    const error = new Error(detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function setBusy(element, busy) {
  element.setAttribute("aria-busy", String(busy));
}

function showNotice(message) {
  $("#noticeText").textContent = message;
  $("#notice").hidden = false;
}

function hideNotice() {
  $("#notice").hidden = true;
}

function handleLoadError(error) {
  if (error && error.status === 429) {
    showNotice("检查请求过于频繁，请稍后再试");
  } else {
    showNotice("数据加载失败，请重试");
  }
}

function renderOverview(data) {
  const status = $("#status");
  const statusText = $("#statusText");
  let label = "等待检查";
  if (data.status === "running") {
    label = data.baseline_at ? "正在检查" : "正在建立基线";
  } else if (data.status === "ok") {
    label = `运行正常 · ${formatRelative(data.last_success_at || data.last_poll_at)}`;
  } else if (data.status === "partial") {
    label = `部分数据源失败 · ${formatRelative(data.last_poll_at)}`;
  } else if (data.status === "error") {
    label = `检查失败 · ${formatRelative(data.last_poll_at)}`;
  } else if (!data.baseline_at) {
    label = "正在连接";
  }
  status.dataset.state = data.status || "idle";
  status.title = data.last_error || "";
  statusText.textContent = label;
  $("#refreshButton").disabled = data.status === "running";

  $("#statModels").textContent = formatCount(data.models);
  $("#statDatasets").textContent = formatCount(data.datasets);
  $("#statEvents").textContent = formatCount(data.events);

  const intervalMinutes = Math.max(1, Math.round((data.poll_interval || 0) / 60));
  $("#footerMeta").textContent = `每 ${intervalMinutes} 分钟检查`;
  if (data.source_url) {
    $("#sourceLink").href = data.source_url;
  }
  updateCategories(data.categories || []);
}

function updateCategories(categories) {
  const eventSelect = $("#eventCategory");
  const previousEvent = state.eventCategory;
  eventSelect.replaceChildren(new Option("全部", "all"));
  for (const category of categories) {
    eventSelect.append(new Option(category.label, category.key));
  }
  if (categories.some((category) => category.key === previousEvent)) {
    eventSelect.value = previousEvent;
  } else {
    eventSelect.value = "all";
    state.eventCategory = "all";
  }

  const boardSelect = $("#boardCategory");
  const previousBoard = state.boardCategory;
  boardSelect.replaceChildren();
  for (const category of categories) {
    boardSelect.append(new Option(category.label, category.key));
  }
  if (categories.some((category) => category.key === previousBoard)) {
    boardSelect.value = previousBoard;
  } else if (categories.length) {
    boardSelect.value = categories[0].key;
    state.boardCategory = categories[0].key;
  } else {
    state.boardCategory = "";
  }
  boardSelect.disabled = categories.length === 0;
}

function headline(parts) {
  const element = document.createElement("div");
  element.className = "event-headline";
  for (const [text, className] of parts) {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    element.append(span);
  }
  return element;
}

function scoreChips(scores) {
  const entries = Object.entries(scores || {});
  if (!entries.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "score-chips";
  for (const [field, value] of entries) {
    const chip = document.createElement("span");
    chip.className = "score-chip";
    const label = document.createElement("span");
    label.textContent = `${field} `;
    const strong = document.createElement("strong");
    strong.textContent = value === "" || value == null ? "—" : value;
    chip.append(label, strong);
    wrap.append(chip);
  }
  return wrap;
}

function renderEvent(item) {
  const article = document.createElement("article");
  article.className = "event";

  const time = document.createElement("time");
  time.className = "event-time";
  time.dateTime = item.created_at;
  time.textContent = formatTime(item.created_at);

  const badge = document.createElement("span");
  badge.className = `event-badge type-${item.type}`;
  badge.textContent = EVENT_LABELS[item.type] || item.type;

  const body = document.createElement("div");
  body.className = "event-body";
  const dataset = item.dataset_label || "";
  const model = item.model_label || item.model || "";
  const details = item.details || {};

  if (item.type === "dataset_added") {
    body.append(headline([[dataset, "event-model"], ["已发布", "event-text"]]));
    if (Number.isFinite(details.row_count)) {
      const detail = document.createElement("div");
      detail.className = "event-detail";
      detail.textContent = `收录 ${details.row_count} 个模型`;
      body.append(detail);
    }
  } else if (item.type === "model_added") {
    body.append(headline([[model, "event-model"], ["加入", "event-text"], [dataset, "event-dataset"]]));
    const chips = scoreChips(details.scores);
    if (chips) body.append(chips);
  } else if (item.type === "score_changed") {
    body.append(headline([[model, "event-model"], ["分数变更", "event-text"]]));
    const source = document.createElement("div");
    source.className = "event-detail";
    source.textContent = dataset;
    body.append(source);
    const list = document.createElement("div");
    list.className = "change-list";
    for (const change of details.changes || []) {
      const row = document.createElement("div");
      row.className = "change";
      const field = document.createElement("span");
      field.className = "change-field";
      field.textContent = change.field;
      const values = document.createElement("span");
      values.className = "change-values";
      const previous = document.createElement("span");
      previous.className = "change-old";
      previous.textContent = change.old === "" || change.old == null ? "—" : change.old;
      const arrow = document.createElement("span");
      arrow.className = "change-arrow";
      arrow.textContent = "→";
      const next = document.createElement("span");
      next.className = "change-new";
      next.textContent = change.new === "" || change.new == null ? "—" : change.new;
      values.append(previous, document.createTextNode(" "), arrow, document.createTextNode(" "), next);
      row.append(field, values);
      list.append(row);
    }
    body.append(list);
  } else if (item.type === "model_removed") {
    body.append(headline([[model, "event-model"], ["移出", "event-text"], [dataset, "event-dataset"]]));
    const chips = scoreChips(details.scores);
    if (chips) body.append(chips);
  }

  article.append(time, badge, body);
  return article;
}

function emptyState(title, detail) {
  const wrap = document.createElement("div");
  wrap.className = "empty";
  const strong = document.createElement("strong");
  strong.textContent = title;
  const text = document.createElement("span");
  text.textContent = detail;
  wrap.append(strong, text);
  return wrap;
}

function renderEvents(data) {
  const container = $("#eventsList");
  container.replaceChildren();

  if (!data.items.length) {
    if (state.eventType !== "all" || state.eventCategory !== "all") {
      container.append(emptyState("该筛选下暂无记录", ""));
    } else if (!state.overview || (!state.overview.baseline_at && state.overview.status === "running")) {
      container.append(emptyState("正在建立基线", "首次检查完成后显示变更记录"));
    } else if (state.overview && !state.overview.baseline_at) {
      container.append(emptyState("基线尚未建立", "等待首次检查"));
    } else {
      const last = state.overview?.last_success_at || state.overview?.last_poll_at;
      container.append(emptyState("暂无变更记录", last ? `上次检查 ${formatRelative(last)}` : ""));
    }
    return;
  }

  let lastDay = "";
  for (const item of data.items) {
    const day = formatDay(item.created_at);
    if (day !== lastDay) {
      const heading = document.createElement("div");
      heading.className = "event-day";
      heading.textContent = day;
      container.append(heading);
      lastDay = day;
    }
    container.append(renderEvent(item));
  }

  if (data.total > data.items.length) {
    const more = document.createElement("p");
    more.className = "events-more";
    more.textContent = `仅显示最近 ${data.items.length} 条，共 ${data.total} 条`;
    container.append(more);
  }
}

function renderBoard(data) {
  state.boardCategory = data.category;
  state.boardDataset = data.dataset ? data.dataset.key : "";

  const categorySelect = $("#boardCategory");
  if (![...categorySelect.options].some((option) => option.value === data.category)) {
    categorySelect.append(new Option(data.category_label, data.category));
  }
  categorySelect.value = data.category;

  const datasetSelect = $("#boardDataset");
  datasetSelect.replaceChildren();
  for (const dataset of data.datasets) {
    datasetSelect.append(new Option(dataset.label, dataset.key));
  }
  datasetSelect.value = data.dataset ? data.dataset.key : "";
  datasetSelect.disabled = data.datasets.length === 0;

  const wrap = $("#boardTableWrap");
  wrap.replaceChildren();
  if (!data.dataset) {
    wrap.append(emptyState("该分类暂无榜单", ""));
    return;
  }
  if (!data.rows.length) {
    wrap.append(emptyState("该榜单暂无数据", ""));
    return;
  }

  const table = document.createElement("table");
  const caption = document.createElement("caption");
  caption.className = "sr-only";
  caption.textContent = `${data.category_label} ${data.dataset.label}`;
  table.append(caption);

  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  const rankHeader = document.createElement("th");
  rankHeader.scope = "col";
  rankHeader.className = "rank";
  rankHeader.textContent = "位次";
  const modelHeader = document.createElement("th");
  modelHeader.scope = "col";
  modelHeader.textContent = "模型";
  headerRow.append(rankHeader, modelHeader);
  for (const field of data.score_fields) {
    const header = document.createElement("th");
    header.scope = "col";
    header.textContent = field;
    headerRow.append(header);
  }
  thead.append(headerRow);
  table.append(thead);

  const tbody = document.createElement("tbody");
  for (const row of data.rows) {
    const tableRow = document.createElement("tr");
    const rank = document.createElement("td");
    rank.className = "rank";
    rank.textContent = row.position;
    const model = document.createElement("td");
    model.className = "model-cell";
    model.textContent = row.model_label || row.model;
    tableRow.append(rank, model);
    for (const field of data.score_fields) {
      const cell = document.createElement("td");
      cell.className = "score";
      const value = row.scores?.[field] ?? "";
      const span = document.createElement("span");
      span.textContent = value === "" ? "—" : value;
      span.title = value;
      cell.append(span);
      tableRow.append(cell);
    }
    tbody.append(tableRow);
  }
  table.append(tbody);
  wrap.append(table);
}

async function loadOverview() {
  const data = await fetchJson("/api/overview");
  state.overview = data;
  renderOverview(data);
}

async function loadEvents() {
  const token = ++state.eventsRequest;
  const container = $("#eventsList");
  setBusy(container, true);
  const params = new URLSearchParams({
    type: state.eventType,
    category: state.eventCategory,
    limit: "50",
  });
  try {
    const data = await fetchJson(`/api/events?${params.toString()}`);
    if (token !== state.eventsRequest) return;
    renderEvents(data);
    hideNotice();
  } catch (error) {
    if (token !== state.eventsRequest) return;
    if (!container.querySelector(".event")) {
      container.replaceChildren(emptyState("无法加载变更记录", "请重试"));
    }
    throw error;
  } finally {
    if (token === state.eventsRequest) setBusy(container, false);
  }
}

async function loadBoard() {
  const token = ++state.boardRequest;
  const wrap = $("#boardTableWrap");
  setBusy(wrap, true);
  const params = new URLSearchParams();
  if (state.boardCategory) params.set("category", state.boardCategory);
  if (state.boardDataset) params.set("dataset", state.boardDataset);
  try {
    const data = await fetchJson(`/api/leaderboard?${params.toString()}`);
    if (token !== state.boardRequest) return;
    renderBoard(data);
    hideNotice();
  } catch (error) {
    if (token !== state.boardRequest) return;
    if (!wrap.querySelector("table")) {
      wrap.replaceChildren(emptyState("无法加载榜单", "请重试"));
    }
    throw error;
  } finally {
    if (token === state.boardRequest) setBusy(wrap, false);
  }
}

function scheduleNext() {
  clearTimeout(state.timer);
  if (document.hidden) return;
  const overview = state.overview;
  let delay = 60000;
  if (!overview) {
    delay = 10000;
  } else if (overview.status === "running" || (!overview.baseline_at && overview.status !== "error")) {
    delay = 4000;
  }
  state.timer = setTimeout(refreshAll, delay);
}

async function refreshAll() {
  if (state.refreshing) return;
  state.refreshing = true;
  clearTimeout(state.timer);
  try {
    await loadOverview();
    if (state.view === "events") {
      await loadEvents();
    } else {
      await loadBoard();
    }
    hideNotice();
  } catch (error) {
    handleLoadError(error);
  } finally {
    state.refreshing = false;
    scheduleNext();
  }
}

function switchView(view) {
  state.view = view;
  const eventsActive = view === "events";
  $("#tabEvents").classList.toggle("is-active", eventsActive);
  $("#tabBoard").classList.toggle("is-active", !eventsActive);
  $("#tabEvents").setAttribute("aria-selected", String(eventsActive));
  $("#tabBoard").setAttribute("aria-selected", String(!eventsActive));
  $("#eventsView").hidden = !eventsActive;
  $("#boardView").hidden = eventsActive;
  if (eventsActive) {
    loadEvents().catch(handleLoadError);
  } else {
    loadBoard().catch(handleLoadError);
  }
}

async function requestRefresh() {
  const button = $("#refreshButton");
  if (state.refreshing || button.disabled) return;
  button.disabled = true;
  button.classList.add("is-loading");
  try {
    await fetchJson("/api/refresh", { method: "POST" });
    $("#status").dataset.state = "running";
    $("#statusText").textContent = "检查已排队";
    setTimeout(() => {
      refreshAll();
    }, 1200);
  } catch (error) {
    handleLoadError(error);
    button.disabled = false;
  } finally {
    setTimeout(() => {
      button.classList.remove("is-loading");
    }, 1500);
  }
}

function bindEvents() {
  $("#tabEvents").addEventListener("click", () => switchView("events"));
  $("#tabBoard").addEventListener("click", () => switchView("board"));
  $(".tabs").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    switchView(state.view === "events" ? "board" : "events");
    $(state.view === "events" ? "#tabEvents" : "#tabBoard").focus();
  });

  $("#typeFilters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-type]");
    if (!button) return;
    state.eventType = button.dataset.type;
    for (const filter of document.querySelectorAll("#typeFilters .filter")) {
      filter.classList.toggle("is-active", filter === button);
    }
    loadEvents().catch(handleLoadError);
  });

  $("#eventCategory").addEventListener("change", (event) => {
    state.eventCategory = event.target.value;
    loadEvents().catch(handleLoadError);
  });

  $("#boardCategory").addEventListener("change", (event) => {
    state.boardCategory = event.target.value;
    state.boardDataset = "";
    loadBoard().catch(handleLoadError);
  });

  $("#boardDataset").addEventListener("change", (event) => {
    state.boardDataset = event.target.value;
    loadBoard().catch(handleLoadError);
  });

  $("#refreshButton").addEventListener("click", requestRefresh);
  $("#retryButton").addEventListener("click", () => {
    hideNotice();
    refreshAll();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.timer);
    } else {
      refreshAll();
    }
  });
}

bindEvents();
refreshAll();
