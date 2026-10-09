/* =====================================================================
   McPilot · 麦门决策局 — 前端交互（Phase 4）
   与后端 /api/* 通信；所有价格 / 营养 / 券均来自后端真实 MCP 结果。
   ===================================================================== */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  store: null,          // 选中的门店 {store_code,name,address,...}
  jobId: null,
  es: null,
  result: null,         // RecommendationPlan dict
  cardIndex: 0,
  timer: null,
  t0: 0,
  lastEventAt: 0,
  hardTimer: null,
};

const STAGE_ORDER = ["store", "menu", "nutrition", "coupon", "resolve", "candidates", "verify", "done"];
const STAGE_LABEL = {
  store: "门店校验", menu: "在售菜单查询", nutrition: "营养数据匹配", coupon: "优惠券核验",
  resolve: "规范名/套餐组成解析", candidates: "候选组合生成", verify: "真实价格试算", done: "生成推荐方案",
};

/* ------------------------------ 视图切换 ------------------------------ */
function show(view) {
  const map = { home: "#view-home", status: "#view-status", result: "#view-result", fail: "#view-fail" };
  Object.entries(map).forEach(([k, sel]) => { $(sel).hidden = k !== view; });
  const stepEls = $$(".steps__item");
  const idx = view === "home" ? 1 : view === "status" ? 2 : view === "result" || view === "fail" ? 3 : 1;
  stepEls.forEach((el) => {
    const n = Number(el.dataset.step);
    el.classList.toggle("is-active", n === idx);
    el.classList.toggle("is-done", n < idx);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, ms);
}

/* ------------------------------ 后端连通性 ------------------------------ */
async function checkBackend() {
  const badge = $("#conn-badge");
  const text = $("#conn-text");
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    if (!r.ok) throw new Error("health " + r.status);
    const cfg = await fetch("/api/config", { cache: "no-store" }).then((x) => x.json());
    if (cfg.mcp_configured) {
      badge.classList.add("is-ok");
      text.textContent = "后端就绪 · MCP 已配置";
    } else {
      badge.classList.add("is-warn");
      text.textContent = "后端就绪 · MCP 未配置";
      toast("后端可用，但未检测到 MCP 凭据，查询会报错。", 4200);
    }
  } catch (e) {
    badge.classList.add("is-bad");
    text.textContent = "后端不可用";
  }
}

/* ------------------------------ 门店查询 ------------------------------ */
async function queryStores() {
  const city = $("#in-city").value.trim();
  const keyword = $("#in-keyword").value.trim();
  const list = $("#store-list");
  if (!city || !keyword) {
    list.innerHTML = '<p class="muted small">请输入城市与关键词后再查询。</p>';
    return;
  }
  const btn = $("#btn-stores");
  btn.disabled = true;
  btn.textContent = "查询中…";
  list.innerHTML = '<p class="muted small">正在真实调用门店接口…</p>';
  try {
    const url = `/api/stores?city=${encodeURIComponent(city)}&keyword=${encodeURIComponent(keyword)}&be_type=1`;
    const r = await fetch(url, { cache: "no-store" });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || "查询失败");
    renderStores(data.stores || [], data.cached);
  } catch (e) {
    list.innerHTML = `<p class="form-error">门店查询失败：${escapeHtml(e.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "查询门店";
  }
}

function renderStores(stores, cached) {
  const list = $("#store-list");
  if (!stores.length) {
    list.innerHTML = '<p class="muted small">未找到门店，请更换关键词（如换用商圈名）。</p>';
    return;
  }
  list.innerHTML = "";
  stores.forEach((s, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "store";
    btn.setAttribute("role", "radio");
    btn.setAttribute("aria-checked", "false");
    const dist = s.distance != null ? `${s.distance} m` : "距离未知";
    const open = s.business_status ? "营业中" : "休息中";
    const badge = `<span class="store__badge ${s.business_status ? "" : "is-closed"}">${open}</span>`;
    btn.innerHTML =
      `<span><span class="store__name">${escapeHtml(s.name || "未命名门店")}${badge}</span>` +
      `<span class="store__addr">${escapeHtml(s.address || "")}</span>` +
      `<span class="store__meta">#${escapeHtml(s.store_code)} · ${dist} · ${s.business_start || "?"}–${s.business_end || "?"}</span></span>`;
    btn.addEventListener("click", () => selectStore(s, btn));
    list.appendChild(btn);
    if (i === 0) selectStore(s, btn);
  });
  if (cached) toast("门店结果来自短缓存（5 分钟内）。", 1800);
}

function selectStore(s, el) {
  state.store = s;
  $$("#store-list .store").forEach((b) => {
    b.classList.remove("is-on");
    b.setAttribute("aria-checked", "false");
  });
  el.classList.add("is-on");
  el.setAttribute("aria-checked", "true");
}

/* ------------------------------ 表单 → 提交 ------------------------------ */
function parseTokens(raw) {
  return String(raw || "")
    .split(/[,，;；、\n\s]+/)
    .map((s) => s.trim())
    .filter((s) => s.length >= 1 && s.length <= 8);
}

function showFormError(msg) {
  const el = $("#form-error");
  el.textContent = msg;
  el.hidden = false;
}

function validateForm() {
  const errs = [];
  const budget = $("#in-budget").value.trim();
  if (budget && !(Number(budget) > 0)) errs.push("预算需为正数。");
  if (!state.store) errs.push("请先查询并选择一个真实门店。");
  const people = Number($("#in-people").value);
  if (!(people >= 1)) errs.push("用餐人数需 ≥ 1。");
  return errs;
}

async function submitRequest() {
  $("#form-error").hidden = true;
  const errs = validateForm();
  if (errs.length) {
    showFormError(errs.join(" "));
    return;
  }
  const likes = parseTokens($("#in-likes").value).concat(parseTokens($("#in-note").value));
  const payload = {
    store_code: state.store.store_code,
    be_type: 1,
    budget: $("#in-budget").value.trim() || null,
    people: Number($("#in-people").value) || 1,
    likes: Array.from(new Set(likes)),
    dislikes: parseTokens($("#in-dislikes").value),
    use_coupon: $("#in-usecoupon").checked,
    goals: {
      energy_kcal_max: $("#in-kcal").value.trim() || null,
      protein_g_min: $("#in-protein").value.trim() || null,
    },
  };

  show("status");
  resetPipeline();
  startTimer();

  try {
    const r = await fetch("/api/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || `请求失败（${r.status}）`);
    state.jobId = data.job_id;
    subscribeJob(data.job_id);
  } catch (e) {
    fail("请求未能受理", e.message, "internal");
  }
}

/* ------------------------------ 进度订阅（SSE，带轮询兜底） ------------------------------ */
function resetPipeline() {
  $$(".stage").forEach((el) => {
    el.classList.remove("is-active", "is-done", "is-fail");
    $(".stage__val", el).textContent = "待处理";
  });
  $("#log").textContent = "";
  $("#status-note").textContent = "日志来自后端真实回调，非前端计时器伪造。";
  state.lastEventAt = Date.now();
}

function startTimer() {
  stopTimer();
  state.t0 = Date.now();
  state.timer = setInterval(() => {
    const s = Math.floor((Date.now() - state.t0) / 1000);
    $("#status-timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    if (Date.now() - state.lastEventAt > 45000) {
      $("#status-note").textContent = "已超过 45 秒未收到新进度，可能网络拥塞或接口超时…";
    }
  }, 500);
  state.hardTimer = setTimeout(() => {
    if (state.es) { fail("查询超时", "后端在 120 秒内未返回结果，可能是网络或接口超时。", "timeout"); }
  }, 120000);
}

function stopTimer() {
  if (state.timer) clearInterval(state.timer);
  if (state.hardTimer) clearTimeout(state.hardTimer);
  state.timer = state.hardTimer = null;
}

function subscribeJob(jobId) {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  state.es = es;

  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    state.lastEventAt = Date.now();
    if (data.type === "heartbeat") return;
    if (data.type === "result") { es.close(); state.es = null; fetchResult(jobId); return; }
    if (data.type === "error") { es.close(); state.es = null; fail("处理失败", data.message || "未知错误", data.kind || "internal"); return; }
    applyEvent(data);
  };
  es.onerror = () => {
    // SSE 断开：改用轮询兜底（后端任务仍在进行）
    es.close();
    state.es = null;
    pollJob(jobId, 0);
  };
}

async function pollJob(jobId, tries) {
  if (tries > 240) { fail("连接中断", "无法从后端获取任务状态。", "network"); return; }
  try {
    const r = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error);
    (data.events || []).forEach(applyEvent);
    if (data.status === "done") { state.result = data.result; return renderResult(data.result); }
    if (data.status === "error") { fail("处理失败", data.error, data.error_kind); return; }
  } catch { /* 继续重试 */ }
  setTimeout(() => pollJob(jobId, tries + 1), 700);
}

function applyEvent(ev) {
  const stage = ev.stage;
  if (!stage) return;
  const el = $(`.stage[data-stage="${stage}"]`);
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const label = STAGE_LABEL[stage] || stage;
  const detail = ev.detail || "";

  if (el) {
    if (ev.status === "start") {
      el.classList.add("is-active");
      $(".stage__val", el).textContent = "进行中…";
    } else if (ev.status === "progress") {
      el.classList.add("is-active");
      $(".stage__val", el).textContent = ev.total ? `${ev.index}/${ev.total}` : "进行中…";
    } else if (ev.status === "done") {
      el.classList.remove("is-active");
      el.classList.add("is-done");
      $(".stage__val", el).textContent = "完成";
    }
    // 标记此前阶段为已完成
    const i = STAGE_ORDER.indexOf(stage);
    STAGE_ORDER.slice(0, i).forEach((s) => {
      const prev = $(`.stage[data-stage="${s}"]`);
      if (prev && !prev.classList.contains("is-done")) {
        prev.classList.remove("is-active");
        prev.classList.add("is-done");
        if ($(".stage__val", prev).textContent === "待处理") $(".stage__val", prev).textContent = "完成";
      }
    });
  }
  const log = $("#log");
  const cls = ev.status === "done" ? "ok" : ev.status === "progress" ? "" : "warn";
  log.textContent += `[${ts}] ${label} · ${ev.status}${detail ? " — " + detail : ""}\n`;
  log.scrollTop = log.scrollHeight;
}

async function fetchResult(jobId) {
  try {
    const r = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    const data = await r.json();
    if (data.status === "done" && data.result) { renderResult(data.result); }
    else if (data.status === "error") { fail("处理失败", data.error, data.error_kind); }
    else { pollJob(jobId, 0); }
  } catch (e) {
    fail("获取结果失败", e.message, "network");
  }
}

/* ------------------------------ 失败 ------------------------------ */
const FAIL_HINT = {
  config: "后端未解析到 MCP 凭据。请按 README 配置 MCP 端点/令牌后重启服务。",
  network: "与 MCP 的网络往返失败。请检查网络后重试。",
  timeout: "接口响应超时。可稍后重试，或放宽条件。",
  server: "MCP 返回了错误。可稍后重试。",
  param: "请求参数不合法，请返回修改后重试。",
  internal: "服务内部错误。请重试或查看服务端日志。",
};

function fail(title, msg, kind) {
  stopTimer();
  if (state.es) { state.es.close(); state.es = null; }
  $("#fail-title").textContent = title;
  $("#fail-code").textContent = (kind || "error").toUpperCase();
  $("#fail-msg").textContent = msg || "未知错误";
  $("#fail-hint").textContent = FAIL_HINT[kind] || "";
  show("fail");
}

/* ------------------------------ 结果渲染 ------------------------------ */
function yuan(n) { return (Number(n) || 0).toFixed(2); }

function renderResult(plan) {
  stopTimer();
  state.result = plan;
  const recs = plan.recommendations || [];

  $("#result-count").textContent = String(recs.length);
  const req = plan.request || {};
  const bits = [];
  if (req.budget != null) bits.push(`预算 ¥${yuan(req.budget)}`);
  bits.push(`${req.people || 1} 人`);
  if ((req.likes || []).length) bits.push(`偏好：${req.likes.join("、")}`);
  if ((req.dislikes || []).length) bits.push(`忌口：${req.dislikes.join("、")}`);
  const g = req.goals || {};
  if (g.energy_kcal_max != null) bits.push(`热量 ≤ ${g.energy_kcal_max} kcal`);
  if (g.protein_g_min != null) bits.push(`蛋白 ≥ ${g.protein_g_min} g`);
  const st = plan.stats || {};
  const called = st.mcp_calls || {};
  $("#result-meta").textContent =
    `门店 #${plan.store_code} ｜ 查询时间 ${plan.queried_at || "—"} ｜ 真实试算 ${st.verified ?? "?"} 次` +
    `（calculate-price ${called["calculate-price"] ?? "?"} 次）｜ 耗时 ${st.elapsed_ms ?? "?"} ms ｜ ${bits.join(" ｜ ")}`;
  $("#result-scope").textContent =
    `数据范围：本次在售 ${st.menu_size ?? "?"} 件、生成候选 ${st.candidates ?? "?"} 个（预算内 ${st.candidates_within_budget ?? "?"} 个）、` +
    `真实试算 ${st.verified ?? "?"} 个；营养按可靠匹配项合计（未知不按 0 计）；券按真实商品编码判定适用性。`;

  const box = $("#cards");
  box.innerHTML = "";

  if (!recs.length) {
    const div = document.createElement("div");
    div.className = "empty-result";
    div.innerHTML =
      "<h3>没有找到符合条件的方案</h3>" +
      '<p class="muted">系统不会用不满足预算 / 忌口 / 营养约束的方案凑数。' +
      "被排除的原因已按类别归入下方<b>「数据说明与限制」</b>，" +
      "更具体的调整方向见 <b>「试试这样调整」</b>。</p>";
    box.appendChild(div);
  } else {
    recs.forEach((r, i) => box.appendChild(renderCard(r, i)));
  }

  const cardBtn = $("#btn-card");
  if (cardBtn) cardBtn.disabled = !recs.length;

  renderAdjustments(plan);
  renderNotes(plan);
  renderDataSource(plan);
  show("result");
}

/* --------------------- 智能调整建议（Phase 4.2） --------------------- */
const FIELD_MAP = {
  "budget": "#in-budget",
  "people": "#in-people",
  "dislikes": "#in-dislikes",
  "likes": "#in-likes",
  "goals.energy_kcal_max": "#in-kcal",
  "goals.protein_g_min": "#in-protein",
};

function renderAdjustments(plan) {
  const box = $("#adjust-box");
  const list = $("#adjust-list");
  list.innerHTML = "";
  const items = (plan.adjustments || []).slice(0, 3);
  if (!items.length) {
    box.hidden = true;
    return;
  }

  items.forEach((a, i) => {
    const li = document.createElement("li");
    li.className = `adj adj--${a.kind || "generic"} adj--${a.severity || "info"}`;

    const no = document.createElement("span");
    no.className = "adj__no";
    no.setAttribute("aria-hidden", "true");
    no.textContent = String(i + 1).padStart(2, "0");
    li.appendChild(no);

    const main = document.createElement("div");
    main.className = "adj__main";

    if (a.field) {
      const head = document.createElement("div");
      head.className = "adj__values";
      head.innerHTML =
        `<span class="adj__field">${escapeHtml(a.field)}</span>` +
        `<span class="adj__cur">${escapeHtml(a.current || "—")}</span>` +
        `<span class="adj__arrow" aria-hidden="true">→</span>` +
        `<span class="adj__new">${escapeHtml(a.suggested || "—")}</span>`;
      main.appendChild(head);
    }

    const reason = document.createElement("p");
    reason.className = "adj__reason";
    reason.textContent = a.reason || "";
    main.appendChild(reason);
    li.appendChild(main);

    const patchKeys = Object.keys(a.patch || {});
    const applicable = patchKeys.some((k) => FIELD_MAP[k]);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn--primary adj__apply";
    btn.textContent = applicable ? "带回表单" : "仅供提示";
    btn.disabled = !applicable;
    if (applicable) btn.addEventListener("click", () => applyAdjustment(a, i + 1));
    li.appendChild(btn);

    list.appendChild(li);
  });

  box.hidden = false;
}

/** 把一条建议的 patch 写回需求表单；**不触发**任何查询。 */
function applyAdjustment(a, index) {
  const applied = [];
  const touched = [];
  Object.entries(a.patch || {}).forEach(([key, val]) => {
    const sel = FIELD_MAP[key];
    if (!sel) return;
    const el = $(sel);
    if (!el) return;
    el.value =
      val === null || val === undefined
        ? ""
        : Array.isArray(val)
          ? val.join(", ")
          : String(val);
    applied.push(key);
    touched.push(el);
  });

  if (!applied.length) {
    toast("该建议不含可自动填写的字段，请手动调整。");
    return;
  }

  syncQuickBudget();
  show("home");
  toast(`已将建议 #${index} 带回表单，请确认后点击「开始决策」。`, 4600);

  touched.forEach((el) => {
    el.classList.add("is-patched");
    setTimeout(() => el.classList.remove("is-patched"), 2800);
  });
  const first = touched[0];
  setTimeout(() => {
    try { first.focus({ preventScroll: true }); } catch { /* 忽略 */ }
    first.scrollIntoView({ block: "center", behavior: "smooth" });
  }, 320);
}

function syncQuickBudget() {
  const v = Number($("#in-budget").value);
  $$(".chip[data-budget]").forEach((x) => x.classList.toggle("is-on", Number(x.dataset.budget) === v));
}

function renderCard(r, idx) {
  const card = document.createElement("article");
  card.className = `rcard rcard--${r.strategy}`;

  const bar = document.createElement("div");
  bar.className = "rcard__bar";
  bar.innerHTML = `<span class="rcard__label">${escapeHtml(r.label)}</span><span class="rcard__tag">#${idx + 1} ${String(r.strategy).toUpperCase()}</span>`;
  card.appendChild(bar);

  const body = document.createElement("div");
  body.className = "rcard__body";

  // 价格（主指标：最醒目）
  const p = r.price || {};
  const price = document.createElement("div");
  price.className = "price";
  price.innerHTML =
    `<span class="price__now">¥${yuan(p.payable_yuan)}</span>` +
    (Number(p.discount_yuan) > 0 ? `<span class="price__orig">¥${yuan(p.original_yuan)}</span><span class="price__save">省 ¥${yuan(p.discount_yuan)}</span>` : "") +
    `<span class="price__unit">实付（官方试算）</span>`;
  body.appendChild(price);

  // 关键指标条：蛋白质 / 热量（次级突出，直接来自可靠营养）
  const n = r.nutrition_complete && r.nutrition ? r.nutrition : null;
  const stats = document.createElement("div");
  stats.className = "keystats";
  const density =
    n && n.protein_g != null && n.energy_kcal ? (Number(n.protein_g) / (Number(n.energy_kcal) / 100)).toFixed(1) : null;
  stats.innerHTML =
    kpi("蛋白质", n && n.protein_g != null ? fmtNum(n.protein_g) : "—", "g", true) +
    kpi("热量", n && n.energy_kcal != null ? fmtNum(n.energy_kcal) : "—", "kcal", true) +
    kpi("蛋白密度", density != null ? density : "—", "g/100kcal", false);
  body.appendChild(stats);

  // 商品（带官方图片）
  const items = document.createElement("ul");
  items.className = "items";
  (r.items || []).forEach((it) => {
    const li = document.createElement("li");
    li.className = "item";
    const img = it.image
      ? `<img class="item__thumb" src="${escapeAttr(it.image)}" alt="${escapeAttr(it.name)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'item__thumb item__thumb--none',textContent:'无图'}))" />`
      : `<span class="item__thumb item__thumb--none">暂无<br/>图片</span>`;
    const nut = it.nutrition;
    let sub;
    if (nut) {
      sub = `${fmtNum(nut.energy_kcal)} kcal · 蛋白 ${fmtNum(nut.protein_g)} g`;
    } else {
      sub = "营养信息缺失";
    }
    li.innerHTML =
      img +
      `<div><span class="item__name">${escapeHtml(it.name)} ×${it.quantity}</span>` +
      `<span class="item__sub ${nut ? "" : "is-missing"}">${escapeHtml(sub)}</span></div>` +
      `<span class="item__price">¥${yuan(it.line_payable_yuan)}</span>`;
    items.appendChild(li);
  });
  body.appendChild(items);

  // 营养明细：默认折叠（四维明细属技术性信息）
  const nutri = document.createElement("details");
  nutri.className = "nutri-fold";
  if (r.nutrition_complete && n) {
    nutri.innerHTML =
      `<summary><span class="nutri-fold__label">◈ 完整营养明细（可靠匹配合计）</span><span class="note__fold" aria-hidden="true">展开 ▾</span></summary>` +
      '<div class="nutri__grid">' +
      cell("热量", fmtNum(n.energy_kcal), "kcal") +
      cell("蛋白质", fmtNum(n.protein_g), "g") +
      cell("脂肪", fmtNum(n.fat_g), "g") +
      cell("碳水", fmtNum(n.carb_g), "g") +
      "</div>";
  } else {
    const miss = (r.nutrition_missing || []).map(escapeHtml).join("、");
    nutri.open = true;
    nutri.innerHTML =
      `<summary><span class="nutri-fold__label is-missing">⚠ 组合营养无法可靠合计</span><span class="note__fold" aria-hidden="true">收起 ▴</span></summary>` +
      `<div class="nutri__none">部分商品未匹配到可靠营养数据：${miss || "未知"}。<br/>为避免误导，此处不给出合计值。</div>`;
  }
  body.appendChild(nutri);

  // 券（如实说明）
  const cp = document.createElement("div");
  const used = r.coupon && Number(p.discount_yuan) > 0;
  cp.className = `coupon ${used ? "coupon--used" : "coupon--none"}`;
  cp.innerHTML =
    `<span class="coupon__mark">${used ? "券已适用" : "未使用券"}</span>` +
    `<span>${escapeHtml(r.coupon_note || "未使用优惠券")}${r.coupon && r.coupon.title ? "：" + escapeHtml(r.coupon.title) : ""}</span>`;
  body.appendChild(cp);

  // 推荐理由（默认展开：可解释性优先）
  if ((r.reasons || []).length) {
    const ul = document.createElement("ul");
    ul.className = "reasons";
    r.reasons.forEach((x) => {
      const li = document.createElement("li");
      li.textContent = x;
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }

  // 评分细节与数据说明：默认折叠（技术性信息，不干扰主决策）
  const detail = document.createElement("details");
  detail.className = "rcard__detail";
  detail.innerHTML =
    `<summary><span>评分与数据说明</span><span class="note__fold" aria-hidden="true">展开 ▾</span></summary>` +
    `<div class="rcard__detail-body">` +
    `<p><b>评分</b>：${Number(r.score || 0).toFixed(3)}（依据真实价格 / 营养 / 偏好等维度加权，明细见推荐理由）</p>` +
    `<p><b>价格</b>：来自官方 calculate-price 试算；展示价仅用于候选筛选，不作为最终价。</p>` +
    `<p><b>营养</b>：仅采用可靠匹配项合计；未知商品不按 0 计，已在上方标注。</p>` +
    `<p><b>数据来源</b>：${escapeHtml(r.data_source || "麦当劳官方 MCP")}｜查询时间 ${escapeHtml(plan_queried_at())}</p>` +
    `</div>`;
  body.appendChild(detail);

  card.appendChild(body);

  const foot = document.createElement("div");
  foot.className = "rcard__foot";
  foot.innerHTML =
    `<span>评分 ${Number(r.score || 0).toFixed(3)}</span>` +
    `<span>候选实付（含券）· 官方试算</span>`;
  card.appendChild(foot);
  return card;
}

function plan_queried_at() {
  return (state.result && state.result.queried_at) || "—";
}

function kpi(label, value, unit, strong) {
  return (
    `<div class="keystat ${strong ? "keystat--strong" : ""}">` +
    `<span class="keystat__k">${label}</span>` +
    `<span class="keystat__v">${value}<small> ${unit}</small></span>` +
    `</div>`
  );
}

function cell(k, v, unit) {
  return `<div class="nutri__cell"><span class="nutri__k">${k}</span><span class="nutri__v">${v}<small> ${unit}</small></span></div>`;
}
function fmtNum(v) { return v == null ? "—" : String(Math.round(Number(v) * 10) / 10); }

function renderNotes(plan) {
  const wl = $("#warn-list");
  wl.innerHTML = "";
  (plan.warnings || []).forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    wl.appendChild(li);
  });
  const wb = $("#warn-box");
  const hasWarn = (plan.warnings || []).length > 0;
  wb.hidden = !hasWarn;
  if (!hasWarn) wb.open = false; // 默认折叠，用户可自行展开
}

function renderDataSource(plan) {
  const st = plan.stats || {};
  const calls = st.mcp_calls || {};
  const blocked = st.blocked || {};
  const blockedLine = Object.keys(blocked).length
    ? `本次阻断归因：${Object.entries(blocked).map(([k, v]) => `${BLOCK_LABEL[k] || k} ${v}`).join("，")}\n`
    : "";
  $("#data-source").textContent =
    `${plan.data_source || ""}\n` +
    `门店 #${plan.store_code} · beType=${plan.be_type} · 查询时间 ${plan.queried_at || "—"}\n` +
    `MCP 调用：${Object.entries(calls).map(([k, v]) => `${k}×${v}`).join("，") || "—"}\n` +
    blockedLine +
    `说明：价格为官方 calculate-price 试算结果；营养仅采用可靠匹配项（未知不按 0 计）；` +
    `优惠券按真实商品编码判定适用性；调整建议仅基于本次已生成并试算的候选范围，不代表整份菜单无解。`;
}

const BLOCK_LABEL = {
  over_budget: "真实实付超预算",
  dislike: "套餐组成命中忌口",
  kcal: "热量超过上限",
  protein: "蛋白质未达下限",
  fat: "脂肪超过上限",
  nutrition_unknown: "营养未知无法确认达标",
  verify_failed: "价格试算失败",
  candidates: "生成候选总数",
  within_budget: "预算内候选数",
};

/* ------------------------------ 决策卡（Canvas → PNG） ------------------------------ */
function openCardDialog() {
  if (!state.result || !(state.result.recommendations || []).length) { toast("暂无可导出的方案。"); return; }
  state.cardIndex = 0;
  const tabs = $("#card-tabs");
  tabs.innerHTML = "";
  state.result.recommendations.forEach((r, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = r.label;
    b.addEventListener("click", () => { state.cardIndex = i; drawCard(); syncTabs(); });
    tabs.appendChild(b);
  });
  syncTabs();
  drawCard();
  const dlg = $("#card-dialog");
  if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
}
function syncTabs() {
  $$("#card-tabs button").forEach((b, i) => b.classList.toggle("is-on", i === state.cardIndex));
}

function drawCard() {
  const plan = state.result;
  const r = plan.recommendations[state.cardIndex];
  const c = $("#card-canvas");
  const W = c.width;
  // 自适应高度：先在临时画布上"空画"一遍量出内容高度，再正式绘制
  const measure = document.createElement("canvas");
  measure.width = W;
  const contentBottom = renderCardCanvas(measure.getContext("2d"), plan, r, W, 100000);
  c.height = Math.max(720, Math.min(2000, contentBottom + 60));
  renderCardCanvas(c.getContext("2d"), plan, r, W, c.height);
}

function renderCardCanvas(g, plan, r, W, H) {
  const INK = "#173B3C", CREAM = "#FFF9E8", TEAL = "#087F85", YEL = "#FFC72C", ORG = "#F58A28";

  g.imageSmoothingEnabled = false;
  g.fillStyle = CREAM; g.fillRect(0, 0, W, H);
  // 像素网格
  g.strokeStyle = "rgba(8,127,133,.09)"; g.lineWidth = 1;
  for (let x = 0; x < W; x += 16) { g.beginPath(); g.moveTo(x + .5, 0); g.lineTo(x + .5, H); g.stroke(); }
  for (let y = 0; y < H; y += 16) { g.beginPath(); g.moveTo(0, y + .5); g.lineTo(W, y + .5); g.stroke(); }
  // 外框
  g.strokeStyle = INK; g.lineWidth = 6; g.strokeRect(14, 14, W - 28, H - 28);

  // 顶栏
  g.fillStyle = TEAL; g.fillRect(17, 17, W - 34, 76);
  g.strokeStyle = INK; g.lineWidth = 6; g.strokeRect(17, 17, W - 34, 76);
  g.fillStyle = CREAM; g.font = "22px 'Press Start 2P', monospace"; g.textBaseline = "middle";
  g.fillText("MCPILOT", 40, 46);
  g.font = "18px 'PingFang SC','Microsoft YaHei',sans-serif";
  g.fillText("麦门决策局 · 决策卡", 40, 74);

  let y = 128;
  // 方案标签
  g.fillStyle = YEL; g.fillRect(40, y - 30, 250, 46);
  g.strokeStyle = INK; g.lineWidth = 4; g.strokeRect(40, y - 30, 250, 46);
  g.fillStyle = INK; g.font = "bold 24px 'PingFang SC','Microsoft YaHei',sans-serif";
  g.fillText(r.label, 56, y - 7);

  // 价格
  y += 54;
  const p = r.price || {};
  g.font = "bold 46px 'Cascadia Code',Consolas,monospace"; g.fillStyle = INK;
  g.fillText(`¥${yuan(p.payable_yuan)}`, 40, y);
  const pw = g.measureText(`¥${yuan(p.payable_yuan)}`).width;
  if (Number(p.discount_yuan) > 0) {
    g.font = "20px 'PingFang SC',sans-serif"; g.fillStyle = "#3d5a5b";
    g.fillText(`原价 ¥${yuan(p.original_yuan)}  省 ¥${yuan(p.discount_yuan)}`, 48 + pw, y);
  }
  y += 44;

  // 分割
  g.strokeStyle = "rgba(23,59,60,.35)"; g.lineWidth = 3; g.setLineDash([8, 6]);
  g.beginPath(); g.moveTo(40, y); g.lineTo(W - 40, y); g.stroke(); g.setLineDash([]);
  y += 34;

  // 商品
  g.font = "bold 20px 'PingFang SC',sans-serif"; g.fillStyle = TEAL;
  g.fillText("组合清单", 40, y); y += 34;
  g.fillStyle = INK;
  (r.items || []).forEach((it) => {
    g.font = "21px 'PingFang SC',sans-serif";
    const name = `${it.name} ×${it.quantity}`;
    g.fillText(name, 52, y);
    g.font = "18px 'Cascadia Code',monospace"; g.textAlign = "right";
    g.fillText(`¥${yuan(it.line_payable_yuan)}`, W - 52, y);
    g.textAlign = "left";
    y += 32;
  });

  y += 14;
  g.strokeStyle = "rgba(23,59,60,.35)"; g.lineWidth = 3; g.setLineDash([8, 6]);
  g.beginPath(); g.moveTo(40, y); g.lineTo(W - 40, y); g.stroke(); g.setLineDash([]);
  y += 34;

  // 营养
  g.font = "bold 20px 'PingFang SC',sans-serif"; g.fillStyle = TEAL;
  g.fillText("营养（可靠匹配合计）", 40, y); y += 32;
  if (r.nutrition_complete && r.nutrition) {
    const n = r.nutrition;
    g.fillStyle = INK; g.font = "21px 'PingFang SC',sans-serif";
    const parts = [
      `${fmtNum(n.energy_kcal)} kcal`,
      `${fmtNum(n.protein_g)} g 蛋白质`,
      `${fmtNum(n.fat_g)} g 脂肪`,
    ];
    parts.forEach((t, i) => g.fillText(t, 52 + i * 210, y));
  } else {
    g.fillStyle = "#c8433a"; g.font = "19px 'PingFang SC',sans-serif";
    const miss = (r.nutrition_missing || []).slice(0, 3).join("、");
    g.fillText(`营养缺失：${miss || "未知商品"}`, 52, y);
  }
  y += 36;

  // 券
  g.fillStyle = INK; g.font = "19px 'PingFang SC',sans-serif";
  const couponTxt = r.coupon && Number(p.discount_yuan) > 0
    ? `券已适用：${r.coupon.title || ""}（-¥${yuan(p.discount_yuan)}）`
    : "未使用优惠券";
  g.fillText(couponTxt, 40, y);
  y += 40;

  // 理由
  g.font = "bold 20px 'PingFang SC',sans-serif"; g.fillStyle = TEAL;
  g.fillText("推荐理由", 40, y); y += 30;
  g.fillStyle = "#3d5a5b"; g.font = "18px 'PingFang SC',sans-serif";
  (r.reasons || []).slice(0, 4).forEach((t) => {
    const line = wrapText(g, t, W - 120);
    line.forEach((ln) => { g.fillText("· " + ln, 44, y); y += 26; });
  });

  // 底部信息（紧跟内容之后，画布高度自适应）
  const footY = y + 8;
  g.strokeStyle = INK; g.lineWidth = 4;
  g.beginPath(); g.moveTo(40, footY - 20); g.lineTo(W - 40, footY - 20); g.stroke();
  g.fillStyle = INK; g.font = "16px 'Cascadia Code',monospace";
  g.fillText(`门店 #${plan.store_code} · beType=${plan.be_type}`, 40, footY + 6);
  g.fillText(`查询时间 ${plan.queried_at || "—"}`, 40, footY + 30);
  g.fillStyle = "#3d5a5b"; g.font = "14px 'PingFang SC',sans-serif";
  g.fillText("数据来自麦当劳官方 MCP（只读）：菜单 / 营养 / 门店券 / 价格试算", 40, footY + 54);
  g.fillStyle = ORG; g.fillRect(W - 150, footY - 2, 110, 6);
  return footY + 62;
}

function wrapText(ctx, text, maxW) {
  const out = [];
  let line = "";
  for (const ch of String(text)) {
    if (ctx.measureText(line + ch).width > maxW && line) { out.push(line); line = ch; }
    else line += ch;
  }
  if (line) out.push(line);
  return out;
}

function downloadCard() {
  const c = $("#card-canvas");
  c.toBlob((blob) => {
    if (!blob) { toast("导出失败，请重试。"); return; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const r = state.result.recommendations[state.cardIndex];
    a.href = url;
    a.download = `mcpilot-decision-${r.strategy}-${state.result.store_code}.png`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast("已导出 PNG 决策卡。");
  }, "image/png");
}

async function copyNote() {
  const r = state.result.recommendations[state.cardIndex];
  const p = r.price || {};
  const txt =
    `McPilot 决策卡\n方案：${r.label}\n门店：#${state.result.store_code}（beType=${state.result.be_type}）\n` +
    `实付：¥${yuan(p.payable_yuan)}（原价 ¥${yuan(p.original_yuan)}，优惠 ¥${yuan(p.discount_yuan)}）\n` +
    `查询时间：${state.result.queried_at || "—"}\n数据来源：${state.result.data_source || "麦当劳官方 MCP"}\n` +
    `说明：价格来自官方 calculate-price 试算；营养仅采用可靠匹配项；券按真实商品编码判定。`;
  try {
    await navigator.clipboard.writeText(txt);
    toast("已复制数据说明。");
  } catch {
    toast("浏览器拒绝了剪贴板访问。");
  }
}

/* ------------------------------ 工具 ------------------------------ */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}
function escapeAttr(s) { return escapeHtml(s); }

/* ------------------------------ 事件绑定 ------------------------------ */
function init() {
  checkBackend();

  $$(".chip[data-budget]").forEach((b) => {
    b.addEventListener("click", () => {
      $("#in-budget").value = b.dataset.budget;
      syncQuickBudget();
    });
  });
  $$(".stepper__btn").forEach((b) => {
    b.addEventListener("click", () => {
      const inp = $("#in-people");
      const v = Math.max(1, Math.min(20, (Number(inp.value) || 1) + Number(b.dataset.people)));
      inp.value = String(v);
    });
  });

  $("#btn-stores").addEventListener("click", queryStores);
  $("#in-keyword").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); queryStores(); } });

  $("#form-req").addEventListener("submit", (e) => { e.preventDefault(); submitRequest(); });

  $("#btn-cancel").addEventListener("click", () => {
    stopTimer();
    if (state.es) { state.es.close(); state.es = null; }
    if (state.jobId) fetch(`/api/jobs/${state.jobId}`).catch(() => {});
    show("home");
  });
  $("#btn-back").addEventListener("click", () => show("home"));
  $("#btn-back-2").addEventListener("click", () => show("home"));
  $("#btn-fail-back").addEventListener("click", () => show("home"));
  $("#btn-retry").addEventListener("click", () => submitRequest());

  $("#btn-card").addEventListener("click", openCardDialog);
  $("#card-close").addEventListener("click", () => { const d = $("#card-dialog"); if (d.close) d.close(); else d.removeAttribute("open"); });
  $("#btn-download").addEventListener("click", downloadCard);
  $("#btn-copy-note").addEventListener("click", copyNote);
}

document.addEventListener("DOMContentLoaded", init);
