import { createComparisonModel, findSourceMentions } from './comparison-data.mjs?v=3.0';
import { escapeHtml as esc, formatDate as date, readable } from './format.mjs?v=1.0';
import { loadPortal } from './data-loader.mjs?v=1.0';

(async () => {
  "use strict";
  const main = document.getElementById("main");
  const articleUrl = article => `#/article/${article.id}`;
  let data;
  main.setAttribute("aria-busy", "true");
  main.innerHTML = '<p class="panel-empty" role="status">뉴스를 불러오고 있어요.</p>';
  try {
    data = await loadPortal();
  } catch (error) {
    console.error("Prism data loading failed:", error);
    main.innerHTML = '<div class="empty-search"><h1>뉴스를 불러오지 못했어요.</h1><p>잠시 후 페이지를 새로고침해 주세요.</p></div>';
    main.removeAttribute("aria-busy");
    return;
  }
  main.removeAttribute("aria-busy");
  const model = createComparisonModel(data);
  const { articles, releases } = model;
  const state = { article: null, releaseId: null, relation: "related", returnFocus: null, homeHash: "#/" };
  const PAGE_SIZE = 24;
  const mobileViewport = window.matchMedia("(max-width: 760px)");
  const image = (article, lazy = true) => `<img src="${esc(article.imageUrl || "assets/images/cover.svg")}" alt="" width="900" height="540" loading="${lazy ? "lazy" : "eager"}" decoding="async" referrerpolicy="no-referrer" data-news-image>`;
  function releaseLink(release) {
    let target;
    try { target = new URL(release.url); } catch { /* Missing links stay plain text. */ }
    return target && ["http:", "https:"].includes(target.protocol) && !target.username && !target.password
      ? `<a class="rss-source" href="${esc(target.href)}" target="_blank" rel="noopener noreferrer">통계자료 원문 ↗</a>` : "";
  }
  function sourceControl(article, release, label = release.title, { inline = false, home = false } = {}) {
    const classes = `statistic-source${inline ? " inline-source" : ""}`;
    const count = model.comparisonCount(article, release.release_id);
    if (!count) return `<span class="${classes} unavailable" title="현재 비교할 다른 기사가 없어요.">${readable(label)}</span>`;
    const description = `${release.title}: 같은 자료를 사용한 기사 ${count}건과 주장 비교`;
    if (home) return `<a class="${classes}" href="${articleUrl(article)}?release=${release.release_id}" aria-label="${esc(description)}">${readable(label)}<span aria-hidden="true"> ↗</span></a>`;
    return `<button type="button" class="${classes}" data-source="${release.release_id}" aria-controls="related-panel" aria-pressed="${state.releaseId === release.release_id}" aria-label="${esc(description)}">${readable(label)}<span aria-hidden="true"> ↗</span></button>`;
  }
  function sourceNames(article, { home = false } = {}) {
    const supported = model.sourcesFor(article);
    if (supported.length) return supported.map(release => sourceControl(article, release, release.title, { home })).join("");
    return model.searchSourcesFor(article).map(release => `<span class="rss-source unverified">${esc(release.title)}</span>`).join("");
  }
  function sourceLabel(article) {
    return model.sourceIds(article).length ? "자료 연결 근거" : "검색 기준 · 자료 사용 미확인";
  }
  function newsCard(article) {
    return `<article class="news-story news-card" data-news-id="${article.id}"><a href="${articleUrl(article)}"><div class="image-wrap">${image(article)}</div><h2>${readable(article.title)}</h2><p class="lead-deck">${readable(article.summary)}</p><div class="byline"><span>${esc(article.publisher)}</span><time datetime="${esc(article.providedAt)}">${date(article.providedAt)}</time></div></a><div class="news-sources"><span>${sourceLabel(article)}</span>${sourceNames(article, { home: true })}</div></article>`;
  }
  function homeLink(params, changes = {}) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) value ? next.set(key, String(value)) : next.delete(key);
    return `#/${next.size ? "?" + next.toString() : ""}`;
  }
  function diversify(items) {
    const queues = new Map();
    for (const item of items) {
      if (!queues.has(item.publisher)) queues.set(item.publisher, []);
      queues.get(item.publisher).push(item);
    }
    const result = [];
    while (result.length < items.length) for (const queue of queues.values()) if (queue.length) result.push(queue.shift());
    return result;
  }
  function home(params) {
    state.article = null;
    state.releaseId = null;
    state.homeHash = location.hash || "#/";
    const query = params.get("q")?.trim() || "";
    const selectedRelease = params.get("release") || "";
    const sort = params.get("sort") === "recent" ? "recent" : "diverse";
    document.getElementById("search").value = query;
    document.title = "Prism · 하나의 자료, 여러 시선";
    let pool = [...articles.values()].filter(article => {
      if (!params.has("updates") && article.duplicateOf && articles.has(article.duplicateOf)) return false;
      if (params.has("evidence") && !model.sourceIds(article).length) return false;
      if (selectedRelease && !model.filterSourceIds(article).includes(selectedRelease)) return false;
      const haystack = `${article.title} ${article.summary} ${article.body} ${article.publisher} ${model.searchSourcesFor(article).map(release => release.title).join(" ")}`.toLowerCase();
      return !query || query.toLowerCase().split(/\s+/).every(term => haystack.includes(term));
    });
    pool.sort((a, b) => b.providedAt.localeCompare(a.providedAt));
    if (sort === "diverse") pool = diversify(pool);
    const pageCount = Math.max(1, Math.ceil(pool.length / PAGE_SIZE));
    const requested = Number(params.get("page")) || 1;
    const page = Math.min(pageCount, Math.max(1, Math.floor(requested)));
    const start = (page - 1) * PAGE_SIZE, selected = pool.slice(start, start + PAGE_SIZE);
    let content = `<div class="home-heading"><div><p class="eyebrow">하나의 통계자료, 여러 시선</p><h1>${query ? `‘${esc(query)}’ 검색 결과` : "통계로 읽는 뉴스"}</h1><p class="home-description">기사 속 통계자료를 선택하고 관련된 주장과 상반된 주장을 비교해 보세요.</p></div><div class="collection-meta">${data.meta.collectedAt ? `<span>수집 ${date(data.meta.collectedAt)}</span>` : ""}<span>전체 기사 ${articles.size}건</span></div></div>
      <div class="news-filters"><label><span class="sr-only">통계자료 선택</span><select id="release-filter"><option value="">모든 통계자료</option>${model.visibleReleases.map(release => `<option value="${release.release_id}" ${selectedRelease === release.release_id ? "selected" : ""}>${esc(release.title)}</option>`).join("")}</select></label><label><span class="sr-only">기사 정렬</span><select id="sort-news"><option value="diverse" ${sort === "diverse" ? "selected" : ""}>다양한 언론사순</option><option value="recent" ${sort === "recent" ? "selected" : ""}>최신순</option></select></label><label class="check-filter"><input type="checkbox" id="source-only" ${params.has("evidence") ? "checked" : ""}>자료 연결 근거가 있는 기사만</label><label class="check-filter"><input type="checkbox" id="show-updates" ${params.has("updates") ? "checked" : ""}>유사 제목 기사 포함</label></div>
      <p class="result-count" role="status">${pool.length}건${pool.length ? ` 중 ${start + 1}–${start + selected.length}건` : ""}</p>`;
    if (!selected.length) content += `<div class="empty-search"><h2>조건에 맞는 기사가 없어요.</h2><p>검색어나 통계자료 필터를 바꿔 보세요.</p><a class="text-button" href="#/">전체 기사 보기</a></div>`;
    else content += `<div class="news-grid">${selected.map(newsCard).join("")}</div>`;
    if (pageCount > 1) {
      content += `<nav class="pagination" aria-label="기사 목록 페이지">${page > 1 ? `<a href="${homeLink(params, { page: page - 1 })}" rel="prev">이전</a>` : '<span aria-disabled="true">이전</span>'}`;
      for (let n = Math.max(1, page - 2); n <= Math.min(pageCount, page + 2); n++) content += `<a href="${homeLink(params, { page: n })}" ${n === page ? 'aria-current="page"' : ""}>${n}</a>`;
      content += `${page < pageCount ? `<a href="${homeLink(params, { page: page + 1 })}" rel="next">다음</a>` : '<span aria-disabled="true">다음</span>'}<span>${page} / ${pageCount}</span></nav>`;
    }
    main.innerHTML = content;
  }
  function articleText(article, text) {
    let result = "", cursor = 0;
    for (const mention of findSourceMentions(text, model.sourcesFor(article))) {
      result += readable(text.slice(cursor, mention.start));
      result += sourceControl(article, mention.release, text.slice(mention.start, mention.end), { inline: true });
      cursor = mention.end;
    }
    return result + readable(text.slice(cursor));
  }
  function bodyHtml(article) {
    if (article.bodyStatus === "selected_excerpt") return `<div class="body-unavailable"><h2>기사 발췌문</h2><blockquote>${readable(article.body)}</blockquote><p>${readable(article.summary)}</p><p class="body-notice">기사의 일부 내용이에요. 전체 내용은 기사 원문에서 확인해 주세요.</p></div>`;
    if (!article.body) return `<div class="body-unavailable"><h2>기사 요약</h2><p>${readable(article.summary)}</p><p class="body-notice">본문을 확보하지 못했어요. 기사 원문에서 전체 내용을 확인해 주세요.</p></div>`;
    return article.body.split(/\r?\n/).filter(line => line.trim()).map(line => `<p>${articleText(article, line)}</p>`).join("");
  }
  function articlePage(id, params) {
    const article = articles.get(id);
    if (state.article?.id !== id) { state.relation = "related"; state.returnFocus = null; }
    state.article = article || null;
    const requested = params.get("release");
    state.releaseId = model.canCompare(article, requested) ? requested : null;
    if (state.releaseId) state.relation = model.relationsFor(article, state.releaseId, "related").length ? "related" : "opposed";
    document.getElementById("search").value = "";
    if (!article) {
      document.title = "기사를 찾을 수 없어요 · Prism";
      main.innerHTML = '<div class="empty-search"><h1>기사를 찾을 수 없어요.</h1><a class="text-button" href="#/">전체 기사 보기</a></div>';
      return;
    }
    document.title = `${article.title} · Prism`;
    main.innerHTML = `<div class="article-topline"><a href="${esc(state.homeHash)}">기사 목록</a><span>/ 뉴스</span></div><div class="reading-layout"><article class="reading-column"><header class="article-header"><h1>${readable(article.title)}</h1><div class="article-meta"><span class="source-name">${esc(article.publisher)}</span>${article.providedAt ? `<time datetime="${esc(article.providedAt)}">발행 ${date(article.providedAt)} ${esc(article.providedAt.slice(11, 16))}</time>` : ""}<a href="${esc(article.url)}" target="_blank" rel="noopener noreferrer">기사 원문 ↗</a></div><div class="article-statistics"><span>${sourceLabel(article)}</span><div>${sourceNames(article)}</div></div></header><figure class="article-visual">${image(article, false)}</figure><section class="article-body" aria-label="기사 본문">${bodyHtml(article)}</section></article><aside class="related-panel" id="related-panel" aria-labelledby="related-heading" tabindex="-1"></aside></div><button type="button" class="mobile-panel-trigger" id="open-panel" ${state.releaseId ? "" : "hidden"}>선택한 통계자료의 주장 비교 ↑</button><button type="button" class="sheet-backdrop" id="sheet-backdrop" tabindex="-1" aria-label="주장 비교 닫기"></button><p id="panel-status" class="sr-only" role="status" aria-live="polite"></p>`;
    renderPanel();
    if (state.releaseId && mobileViewport.matches) openPanel();
  }
  function relationItems() {
    return model.relationsFor(state.article, state.releaseId, state.relation);
  }
  function renderPanel() {
    const items = relationItems();
    const opposed = state.relation === "opposed";
    const panel = document.getElementById("related-panel");
    const release = releases.get(state.releaseId);
    if (!release) {
      panel.innerHTML = '<div class="panel-heading"><h2 id="related-heading">통계자료로 주장 비교</h2></div><div class="panel-empty"><p>기사 속 통계자료를 선택해 주세요. 같은 자료를 사용한 기사들의 주장을 비교할 수 있어요.</p></div>';
      document.getElementById("panel-status").textContent = "";
      return;
    }
    panel.innerHTML = `<div class="panel-heading"><h2 id="related-heading">주장 비교</h2><button id="close-panel" class="panel-close" type="button" aria-label="주장 비교 닫기">×</button></div><p class="selected-source">${readable(release.title)}</p><div class="relation-tabs" role="tablist" aria-label="비교할 주장"><button type="button" id="tab-related" role="tab" aria-selected="${!opposed}" aria-controls="related-list" tabindex="${opposed ? -1 : 0}">관련된 주장</button><button type="button" id="tab-opposed" role="tab" aria-selected="${opposed}" aria-controls="related-list" tabindex="${opposed ? 0 : -1}">상반된 주장</button></div><div id="related-list" role="tabpanel" aria-labelledby="tab-${state.relation}">${items.length ? items.map(item => {
      const article = articles.get(item.articleId);
      return `<article class="related-card"><a href="${articleUrl(article)}"><div class="image-wrap">${image(article)}</div><h3>${readable(article.title)}</h3><div class="byline"><span>${esc(article.publisher)}</span><time>${date(article.providedAt)}</time></div></a><button type="button" class="compare-button" data-compare="${article.id}">두 주장 나란히 보기 ↗</button></article>`;
    }).join("") : `<div class="panel-empty"><h3>${opposed ? "상반된 주장 근거를 찾지 못했어요." : "비교할 기사 근거가 아직 부족해요."}</h3><p>${opposed ? "분석한 기사와 확보한 본문 범위에서 찾은 결과예요. 실제로 상반된 기사가 없다는 뜻은 아니에요." : "본문을 확보하고 자료 연결 근거를 찾으면 여기에 표시돼요."}</p></div>`}</div>`;
    document.getElementById("panel-status").textContent = `${release.title}: ${opposed ? "상반된 주장" : "관련된 주장"} ${items.length}건`;
  }
  function updateSourceControls() {
    document.querySelectorAll("button[data-source]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.source === state.releaseId)));
    const trigger = document.getElementById("open-panel");
    if (trigger) trigger.hidden = !state.releaseId;
  }
  function selectSource(releaseId, trigger) {
    if (!model.canCompare(state.article, releaseId)) return;
    state.releaseId = releaseId;
    state.relation = model.relationsFor(state.article, releaseId, "related").length ? "related" : "opposed";
    state.returnFocus = trigger;
    updateSourceControls();
    renderPanel();
    openPanel();
  }
  function dismissPanel() {
    if (mobileViewport.matches) { closePanel(); return; }
    state.releaseId = null;
    updateSourceControls();
    renderPanel();
    state.returnFocus?.focus({ preventScroll: true });
  }
  function openComparison(id) {
    const item = relationItems().find(entry => entry.articleId === id);
    const other = articles.get(id), current = state.article;
    if (!item || !other || !current) return;
    const column = (article, quote) => `<article><header class="comparison-article-heading"><div class="byline"><span>${esc(article.publisher)}</span><time>${date(article.providedAt)}</time></div><h3><a href="${articleUrl(article)}">${readable(article.title)}</a></h3></header><div class="comparison-thumbnail">${image(article, false)}</div><figure class="comparison-excerpt"><figcaption>기사 발췌문${article.speaker ? ` · ${esc(article.speaker)}` : ""}</figcaption><blockquote cite="${esc(article.url)}">${readable(quote)}</blockquote></figure><a class="text-button" href="${esc(article.url)}" target="_blank" rel="noopener noreferrer">기사 원문 ↗</a></article>`;
    const release = releases.get(state.releaseId);
    document.getElementById("comparison-content").innerHTML = `<p class="comparison-context">${esc(item.qualification || item.reason || "")}</p><div class="comparison-columns">${column(current, item.currentEvidence)}${column(other, item.otherEvidence)}</div><div class="comparison-sources"><h3>선택한 통계자료</h3><span>${esc(release.title)}</span>${releaseLink(release)}</div>`;
    document.getElementById("comparison-title").textContent = state.relation === "opposed" ? "상반된 주장 비교" : "관련된 주장 비교";
    document.getElementById("comparison-dialog").showModal();
  }
  function toggleInert(value) {
    [".site-header", ".site-footer", ".article-topline", ".reading-column", "#open-panel"].forEach(selector => {
      const element = document.querySelector(selector);
      if (element) {
        element.inert = value;
        if (!value) element.removeAttribute("inert");
      }
    });
  }
  function openPanel() {
    if (!state.releaseId) return;
    const panel = document.getElementById("related-panel");
    if (!mobileViewport.matches) { panel?.scrollIntoView({ block: "nearest" }); panel?.focus({ preventScroll: true }); return; }
    if (document.activeElement?.id === "open-panel") state.returnFocus = document.activeElement;
    if (!state.returnFocus) state.returnFocus = document.querySelector(`button[data-source="${state.releaseId}"]`);
    document.body.classList.add("sheet-open");
    toggleInert(true);
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    document.getElementById("close-panel").focus({ preventScroll: true });
  }
  function closePanel(restoreFocus = true) {
    const wasOpen = document.body.classList.contains("sheet-open");
    document.body.classList.remove("sheet-open");
    toggleInert(false);
    const panel = document.getElementById("related-panel");
    panel?.removeAttribute("role"); panel?.removeAttribute("aria-modal");
    if (restoreFocus && wasOpen) state.returnFocus?.focus({ preventScroll: true });
  }
  function route() {
    if (location.hash === "#main") { main.focus(); return; }
    closePanel(false);
    const dialog = document.getElementById("comparison-dialog");
    if (dialog.open) dialog.close();
    const [path, query = ""] = location.hash.slice(1).split("?");
    const match = /^\/article\/([a-f0-9]{20})$/.exec(path);
    if (match) articlePage(match[1], new URLSearchParams(query)); else home(new URLSearchParams(query));
    window.scrollTo(0, 0);
  }
  document.addEventListener("error", event => {
    const target = event.target;
    if (!target.matches?.("img[data-news-image]") || target.dataset.fallback) return;
    target.dataset.fallback = "true"; target.src = "assets/images/cover.svg";
  }, true);
  document.addEventListener("click", event => {
    const button = event.target.closest("button"), id = button?.id;
    if (button?.dataset.source) selectSource(button.dataset.source, button);
    if (id === "tab-related" || id === "tab-opposed") { state.relation = id.slice(4); renderPanel(); document.getElementById(id).focus(); }
    if (button?.dataset.compare) openComparison(button.dataset.compare);
    if (id === "comparison-close") document.getElementById("comparison-dialog").close();
    if (id === "open-panel") openPanel();
    if (id === "close-panel") dismissPanel();
    if (id === "sheet-backdrop") closePanel();
    if (id === "footer-info") document.getElementById("info-dialog").showModal();
    if (id === "info-close") document.getElementById("info-dialog").close();
  });
  document.addEventListener("change", event => {
    const changes = { "release-filter": "release", "sort-news": "sort", "show-updates": "updates", "source-only": "evidence" };
    if (!(event.target.id in changes)) return;
    const value = event.target.type === "checkbox" ? (event.target.checked ? "1" : "") : event.target.value;
    location.hash = homeLink(new URLSearchParams(location.hash.split("?")[1] || ""), { [changes[event.target.id]]: value, page: "" }).slice(1);
  });
  document.addEventListener("keydown", event => {
    if (event.target.getAttribute?.("role") === "tab" && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault(); state.relation = event.key === "Home" ? "related" : event.key === "End" ? "opposed" : state.relation === "related" ? "opposed" : "related";
      renderPanel(); document.getElementById("tab-" + state.relation).focus(); return;
    }
    if (document.getElementById("comparison-dialog").open || document.getElementById("info-dialog").open) return;
    if (event.key === "Escape" && state.releaseId && !mobileViewport.matches) { event.preventDefault(); dismissPanel(); return; }
    if (!document.body.classList.contains("sheet-open")) return;
    if (event.key === "Escape") { event.preventDefault(); closePanel(); return; }
    if (event.key !== "Tab") return;
    const items = [...document.querySelectorAll('#related-panel a[href], #related-panel button:not([tabindex="-1"])')].filter(element => element.getClientRects().length);
    const first = items[0], last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  document.getElementById("search-form").addEventListener("submit", event => {
    event.preventDefault();
    const params = state.article ? new URLSearchParams() : new URLSearchParams(location.hash.split("?")[1] || "");
    location.hash = homeLink(params, { q: document.getElementById("search").value.trim(), page: "" }).slice(1);
  });
  window.addEventListener("hashchange", route);
  const restoreDesktop = () => { if (!window.matchMedia("(max-width: 760px)").matches) closePanel(); };
  const viewportChanged = () => { restoreDesktop(); window.requestAnimationFrame?.(restoreDesktop); };
  mobileViewport.addEventListener("change", viewportChanged);
  window.addEventListener("resize", viewportChanged);
  window.addEventListener("focus", viewportChanged);
  window.visualViewport?.addEventListener("resize", viewportChanged);
  if (typeof ResizeObserver === "function") new ResizeObserver(viewportChanged).observe(document.documentElement);
  route();
})();
