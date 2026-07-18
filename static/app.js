(() => {
  'use strict';

  const RANGE_LABELS = { '1d': '1天', '3d': '3天', '7d': '7天', '30d': '30天', '90d': '90天', '180d': '180天', '1y': '1年', all: '全部' };
  const DIRECTION_COLORS = { bullish: '#20b486', bearish: '#ef5b68', neutral: '#f1b84b' };
  const DEFAULT_PATTERNS = [
    'double_top', 'double_bottom', 'head_shoulders', 'inverse_head_shoulders',
    'bull_flag', 'bear_flag', 'bull_pennant', 'bear_pennant',
    'ascending_triangle', 'descending_triangle', 'symmetrical_triangle',
    'rising_wedge', 'falling_wedge', 'rectangle',
    'elliott_impulse_bull', 'elliott_impulse_bear',
    'elliott_correction_bull', 'elliott_correction_bear'
  ];

  const state = {
    bootstrap: null,
    interval: localStorage.getItem('gatePattern.interval') || '15m',
    range: localStorage.getItem('gatePattern.range') || '30d',
    selectedPatterns: new Set(JSON.parse(localStorage.getItem('gatePattern.patterns') || JSON.stringify(DEFAULT_PATTERNS))),
    currentInstrument: null,
    currentWatch: null,
    watchlist: [],
    rules: [],
    bars: [],
    barMap: new Map(),
    events: [],
    performance: null,
    performanceHorizon: Number(localStorage.getItem('gatePattern.performanceHorizon') || '20'),
    activeEventId: null,
    marketFilter: 'all',
    searchTimer: null,
    alertPollTimer: null,
    lastAlertId: Number(localStorage.getItem('gatePattern.lastAlertId') || '0'),
    loading: false,
    scanning: false,
    chartReady: false,
    markerPrimitive: null,
    opportunitySettings: null,
    opportunityData: null,
    opportunityPollTimer: null,
  };

  const dom = {};
  const chart = {};

  function $(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
  }

  function fmt(value, digits = 4) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--';
    return number.toLocaleString('zh-CN', { maximumFractionDigits: digits });
  }

  function fmtTime(ts) {
    if (!ts) return '--';
    return new Date(Number(ts) * 1000).toLocaleString('zh-CN', { hour12: false });
  }


  function fmtPct(value, digits = 1) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--';
    return `${(number * 100).toFixed(digits)}%`;
  }

  function fmtSignedPct(value, digits = 2) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--';
    const sign = number > 0 ? '+' : '';
    return `${sign}${(number * 100).toFixed(digits)}%`;
  }

  function signalDirection(event) {
    return event && (event.signal_direction || event.direction) || 'neutral';
  }

  function sampleQualityLabel(value) {
    return value === 'high' ? '样本较充分' : value === 'medium' ? '样本一般' : value === 'low' ? '小样本' : '无样本';
  }

  function marketLabel(item) {
    if (!item) return '--';
    if (item.market === 'spot') return item.is_stock ? '股票候选/现货' : '现货';
    return `${String(item.settle || '').toUpperCase()}永续`;
  }

  function showToast(message, error = false) {
    dom.toast.textContent = message;
    dom.toast.classList.toggle('error', error);
    dom.toast.classList.add('show');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => dom.toast.classList.remove('show'), 4200);
  }

  async function api(url, options = {}) {
    const config = { ...options, headers: { ...(options.headers || {}) } };
    if (config.body && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }
    const response = await fetch(url, config);
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch (_) { payload = text; }
    }
    if (!response.ok) {
      const message = payload && payload.detail ? payload.detail : String(payload || `HTTP ${response.status}`);
      throw new Error(message);
    }
    return payload;
  }

  function debounceSearch() {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(searchInstruments, 260);
  }

  async function ensureChartLibrary() {
    if (window.LightweightCharts) return;
    await new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.js';
      script.onload = resolve;
      script.onerror = () => reject(new Error('Lightweight Charts 加载失败'));
      document.head.appendChild(script);
    });
  }

  function addCandlestickSeries(chartApi, options) {
    if (typeof chartApi.addCandlestickSeries === 'function') return chartApi.addCandlestickSeries(options);
    return chartApi.addSeries(LightweightCharts.CandlestickSeries, options);
  }

  function addHistogramSeries(chartApi, options) {
    if (typeof chartApi.addHistogramSeries === 'function') return chartApi.addHistogramSeries(options);
    return chartApi.addSeries(LightweightCharts.HistogramSeries, options);
  }

  function chartOptions(container) {
    return {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: { background: { type: 'solid', color: '#111722' }, textColor: '#8d9ab0', attributionLogo: true },
      grid: { vertLines: { color: '#1c2533' }, horzLines: { color: '#1c2533' } },
      rightPriceScale: { borderColor: '#263246', scaleMargins: { top: 0.08, bottom: 0.24 } },
      timeScale: { borderColor: '#263246', timeVisible: true, secondsVisible: false, rightOffset: 4, barSpacing: 7, minBarSpacing: 0.7 },
      crosshair: {
        mode: LightweightCharts.CrosshairMode ? LightweightCharts.CrosshairMode.Normal : 0,
        vertLine: { color: '#59677c', style: 2, labelBackgroundColor: '#2c394c' },
        horzLine: { color: '#59677c', style: 2, labelBackgroundColor: '#2c394c' },
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    };
  }

  async function initChart() {
    await ensureChartLibrary();
    const container = dom.mainChart;
    chart.api = LightweightCharts.createChart(container, chartOptions(container));
    chart.candles = addCandlestickSeries(chart.api, {
      upColor: '#20b486', downColor: '#ef5b68', borderUpColor: '#20b486', borderDownColor: '#ef5b68',
      wickUpColor: '#20b486', wickDownColor: '#ef5b68', priceLineVisible: true, lastValueVisible: true,
    });
    chart.volume = addHistogramSeries(chart.api, {
      priceFormat: { type: 'volume' }, priceScaleId: '', lastValueVisible: false, priceLineVisible: false,
    });
    chart.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    chart.api.subscribeCrosshairMove(param => {
      const candle = param.seriesData && param.seriesData.get(chart.candles);
      if (!candle) {
        updateLegend(state.bars[state.bars.length - 1]);
        return;
      }
      dom.ohlcLegend.textContent = `O ${fmt(candle.open)}  H ${fmt(candle.high)}  L ${fmt(candle.low)}  C ${fmt(candle.close)}`;
    });
    chart.api.timeScale().subscribeVisibleLogicalRangeChange(() => requestAnimationFrame(drawPatternOverlay));
    chart.api.timeScale().subscribeVisibleTimeRangeChange(() => requestAnimationFrame(drawPatternOverlay));
    container.addEventListener('dblclick', resetChart);
    new ResizeObserver(entries => {
      for (const entry of entries) {
        chart.api.applyOptions({ width: entry.contentRect.width, height: entry.contentRect.height });
        resizeOverlay();
        drawPatternOverlay();
      }
    }).observe(dom.chartContainer);
    state.chartReady = true;
  }

  function resizeOverlay() {
    const rect = dom.chartContainer.getBoundingClientRect();
    dom.patternOverlay.setAttribute('viewBox', `0 0 ${rect.width} ${rect.height}`);
    dom.patternOverlay.setAttribute('width', String(rect.width));
    dom.patternOverlay.setAttribute('height', String(rect.height));
  }

  function resetChart() {
    if (!state.chartReady || !state.bars.length) return;
    chart.api.timeScale().fitContent();
    requestAnimationFrame(drawPatternOverlay);
  }

  function updateLegend(bar) {
    if (!bar) {
      dom.ohlcLegend.textContent = 'O -- H -- L -- C --';
      return;
    }
    dom.ohlcLegend.textContent = `O ${fmt(bar.open)}  H ${fmt(bar.high)}  L ${fmt(bar.low)}  C ${fmt(bar.close)}`;
  }

  function setLoading(active, text = '正在更新行情…') {
    state.loading = active;
    dom.chartLoading.classList.toggle('hidden', !active);
    dom.chartLoading.querySelector('span').textContent = text;
    dom.refreshCurrent.disabled = active;
  }

  function createToolbarButtons() {
    dom.intervalButtons.innerHTML = '';
    state.bootstrap.intervals.forEach(value => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = value.toUpperCase();
      button.dataset.value = value;
      button.classList.toggle('active', value === state.interval);
      button.addEventListener('click', () => changeInterval(value));
      dom.intervalButtons.appendChild(button);
    });
    dom.rangeButtons.innerHTML = '';
    state.bootstrap.ranges.forEach(value => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = RANGE_LABELS[value] || value;
      button.dataset.value = value;
      button.classList.toggle('active', value === state.range);
      button.addEventListener('click', () => changeRange(value));
      dom.rangeButtons.appendChild(button);
    });
  }

  function updateToolbarButtons() {
    [...dom.intervalButtons.children].forEach(btn => btn.classList.toggle('active', btn.dataset.value === state.interval));
    [...dom.rangeButtons.children].forEach(btn => btn.classList.toggle('active', btn.dataset.value === state.range));
  }

  async function changeInterval(value) {
    if (value === state.interval || state.loading) return;
    state.interval = value;
    localStorage.setItem('gatePattern.interval', value);
    updateToolbarButtons();
    if (state.currentWatch) {
      try {
        state.currentWatch = await api(`/api/watchlist/${state.currentWatch.id}`, { method: 'PATCH', body: { default_interval: value } });
        await refreshWatchlist();
      } catch (error) { showToast(error.message, true); }
    }
    await loadCurrentMarket(true);
  }

  async function changeRange(value) {
    if (value === state.range || state.loading) return;
    state.range = value;
    localStorage.setItem('gatePattern.range', value);
    updateToolbarButtons();
    await loadCurrentMarket(true);
  }

  async function searchInstruments() {
    const q = encodeURIComponent(dom.instrumentSearch.value.trim());
    const market = encodeURIComponent(state.marketFilter);
    try {
      const payload = await api(`/api/instruments?q=${q}&market=${market}&limit=160`);
      renderInstrumentResults(payload.items || []);
    } catch (error) {
      dom.instrumentResults.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    }
  }

  function renderInstrumentResults(items) {
    if (!items.length) {
      dom.instrumentResults.innerHTML = '<div class="empty-state">没有匹配的交易标的</div>';
      return;
    }
    const watched = new Set(state.watchlist.map(item => item.instrument_id));
    dom.instrumentResults.innerHTML = items.map(item => `
      <div class="instrument-item ${state.currentInstrument && state.currentInstrument.id === item.id ? 'active' : ''}" data-instrument-id="${escapeHtml(item.id)}">
        <div class="instrument-main">
          <div class="instrument-symbol"><span class="market-tag ${item.is_stock ? 'stock' : ''}">${escapeHtml(marketLabel(item))}</span>${escapeHtml(item.symbol)}</div>
          <div class="instrument-name">${escapeHtml(item.display_name)}</div>
        </div>
        <div class="instrument-actions">
          <button data-open="${escapeHtml(item.id)}">打开</button>
          <button data-watch="${escapeHtml(item.id)}" ${watched.has(item.id) ? 'disabled' : ''}>${watched.has(item.id) ? '已加入' : '+自选'}</button>
        </div>
      </div>`).join('');
    dom.instrumentResults.querySelectorAll('[data-open]').forEach(button => button.addEventListener('click', event => {
      event.stopPropagation();
      openInstrumentById(button.dataset.open);
    }));
    dom.instrumentResults.querySelectorAll('[data-watch]').forEach(button => button.addEventListener('click', async event => {
      event.stopPropagation();
      await addWatch(button.dataset.watch);
    }));
    dom.instrumentResults.querySelectorAll('.instrument-item').forEach(item => item.addEventListener('dblclick', () => openInstrumentById(item.dataset.instrumentId)));
  }

  async function openInstrumentById(id) {
    let instrument = state.watchlist.find(item => item.instrument_id === id);
    if (instrument) {
      await selectWatch(instrument);
      return;
    }
    try {
      const payload = await api(`/api/instruments?q=${encodeURIComponent(id.split(':').pop())}&market=all&limit=100`);
      const found = payload.items.find(item => item.id === id);
      if (!found) throw new Error('未找到交易标的');
      state.currentInstrument = found;
      state.currentWatch = null;
      updateCurrentHeader();
      await loadCurrentMarket(true);
    } catch (error) { showToast(error.message, true); }
  }

  async function addWatch(instrumentId) {
    try {
      const item = await api('/api/watchlist', {
        method: 'POST',
        body: {
          instrument_id: instrumentId,
          default_interval: state.interval,
          refresh_seconds: state.bootstrap.config.default_watch_refresh_seconds,
          selected_patterns: [...state.selectedPatterns],
        },
      });
      showToast(`${item.symbol} 已加入自选`);
      await refreshWatchlist();
      await searchInstruments();
      await selectWatch(item);
    } catch (error) { showToast(error.message, true); }
  }

  async function refreshWatchlist() {
    state.watchlist = await api('/api/watchlist');
    dom.watchCount.textContent = state.watchlist.length;
    renderWatchlist();
    populateAlertInstruments();
  }

  function renderWatchlist() {
    if (!state.watchlist.length) {
      dom.watchlist.innerHTML = '<div class="empty-state">还没有自选标的</div>';
      return;
    }
    dom.watchlist.innerHTML = state.watchlist.map(item => `
      <div class="instrument-item ${state.currentInstrument && state.currentInstrument.id === item.instrument_id ? 'active' : ''}" data-watch-id="${item.id}">
        <div class="instrument-main">
          <div class="instrument-symbol"><span class="market-tag ${item.is_stock ? 'stock' : ''}">${escapeHtml(marketLabel(item))}</span>${escapeHtml(item.symbol)}</div>
          <div class="instrument-name">${escapeHtml(item.display_name)} · ${escapeHtml(item.default_interval.toUpperCase())} · ${item.refresh_seconds}s更新</div>
        </div>
        <div class="instrument-actions">
          <div class="last-mini">${item.last_price == null ? '--' : fmt(item.last_price)}<br>${item.last_ts ? new Date(item.last_ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : ''}</div>
          <button data-remove-watch="${item.id}" title="移除自选">×</button>
        </div>
      </div>`).join('');
    dom.watchlist.querySelectorAll('.instrument-item').forEach(element => {
      element.addEventListener('click', () => {
        const item = state.watchlist.find(row => row.id === Number(element.dataset.watchId));
        if (item) selectWatch(item);
      });
    });
    dom.watchlist.querySelectorAll('[data-remove-watch]').forEach(button => button.addEventListener('click', async event => {
      event.stopPropagation();
      try {
        await api(`/api/watchlist/${button.dataset.removeWatch}`, { method: 'DELETE' });
        if (state.currentWatch && state.currentWatch.id === Number(button.dataset.removeWatch)) state.currentWatch = null;
        await refreshWatchlist();
        showToast('已移除自选');
      } catch (error) { showToast(error.message, true); }
    }));
  }

  async function selectWatch(item) {
    state.currentWatch = item;
    state.currentInstrument = {
      id: item.instrument_id, market: item.market, settle: item.settle, symbol: item.symbol,
      display_name: item.display_name, base: item.base, quote: item.quote, is_stock: item.is_stock,
    };
    if (item.default_interval) state.interval = item.default_interval;
    if (Array.isArray(item.selected_patterns) && item.selected_patterns.length) {
      state.selectedPatterns = new Set(item.selected_patterns);
      persistPatterns();
      renderPatternOptions();
    }
    updateToolbarButtons();
    updateCurrentHeader();
    renderWatchlist();
    await loadCurrentMarket(true);
  }

  function updateCurrentHeader() {
    const item = state.currentInstrument;
    if (!item) return;
    dom.currentSymbol.textContent = item.symbol;
    dom.currentName.textContent = item.display_name;
    dom.currentMarket.textContent = marketLabel(item);
    dom.stockPill.classList.toggle('hidden', !item.is_stock);
    dom.chartEmpty.classList.add('hidden');
  }

  async function loadCurrentMarket(fit = false) {
    if (!state.currentInstrument || state.loading) return;
    setLoading(true);
    try {
      const payload = await api(`/api/candles?instrument_id=${encodeURIComponent(state.currentInstrument.id)}&interval=${state.interval}&range=${state.range}`);
      state.bars = payload.candles || [];
      state.barMap = new Map(state.bars.map(row => [Number(row.ts), row]));
      const candleData = state.bars.map(row => ({ time: Number(row.ts), open: Number(row.open), high: Number(row.high), low: Number(row.low), close: Number(row.close) }));
      const volumeData = state.bars.map(row => ({ time: Number(row.ts), value: Number(row.volume), color: Number(row.close) >= Number(row.open) ? 'rgba(32,180,134,.35)' : 'rgba(239,91,104,.35)' }));
      chart.candles.setData(candleData);
      chart.volume.setData(volumeData);
      const latest = state.bars[state.bars.length - 1];
      updateLegend(latest);
      dom.lastPrice.textContent = latest ? fmt(latest.close, 8) : '--';
      dom.lastTime.textContent = latest ? fmtTime(latest.ts) : '--';
      const note = payload.truncated ? 'Gate 历史根数受限，已显示可获取部分；本地会继续积累。' : '已同步本地 SQLite';
      dom.dataStatus.textContent = `${note} 共 ${state.bars.length} 根，新增/更新 ${payload.fetched || 0} 根${payload.warning ? `；${payload.warning}` : ''}`;
      if (fit) chart.api.timeScale().fitContent();
      await scanCurrentPatterns();
      renderWatchlist();
    } catch (error) {
      showToast(error.message, true);
      dom.dataStatus.textContent = error.message;
    } finally {
      setLoading(false);
    }
  }

  function persistPatterns() {
    localStorage.setItem('gatePattern.patterns', JSON.stringify([...state.selectedPatterns]));
    dom.selectedPatternCount.textContent = state.selectedPatterns.size;
    dom.drawerSelectedText.textContent = `已选择 ${state.selectedPatterns.size} 个`;
  }

  function renderPatternOptions(filter = '') {
    const catalog = state.bootstrap.pattern_catalog;
    const groups = new Map();
    const needle = filter.trim().toLowerCase();
    catalog.forEach(item => {
      if (needle && !(`${item.name} ${item.id} ${item.group}`).toLowerCase().includes(needle)) return;
      if (!groups.has(item.group)) groups.set(item.group, []);
      groups.get(item.group).push(item);
    });
    dom.patternOptions.innerHTML = [...groups.entries()].map(([group, items]) => `
      <section class="pattern-group"><h3>${escapeHtml(group)}</h3><div class="pattern-grid">
        ${items.map(item => `<label class="pattern-option ${state.selectedPatterns.has(item.id) ? 'checked' : ''}">
          <input type="checkbox" value="${escapeHtml(item.id)}" ${state.selectedPatterns.has(item.id) ? 'checked' : ''}>
          <span class="direction-${item.direction}">${escapeHtml(item.name)}</span>
          <small>${item.experimental ? '实验' : item.direction === 'bullish' ? '看涨' : item.direction === 'bearish' ? '看跌' : '中性'}</small>
        </label>`).join('')}
      </div></section>`).join('');
    dom.patternOptions.querySelectorAll('.pattern-option input').forEach(input => input.addEventListener('change', () => {
      if (input.checked) state.selectedPatterns.add(input.value); else state.selectedPatterns.delete(input.value);
      input.closest('.pattern-option').classList.toggle('checked', input.checked);
      persistPatterns();
    }));
    renderAlertPatternOptions();
    persistPatterns();
  }

  function openPatternDrawer() {
    dom.patternDrawer.classList.remove('hidden');
    dom.patternSearch.focus();
  }

  function closePatternDrawer() { dom.patternDrawer.classList.add('hidden'); }

  async function applyPatterns() {
    persistPatterns();
    if (state.currentWatch) {
      try {
        state.currentWatch = await api(`/api/watchlist/${state.currentWatch.id}`, { method: 'PATCH', body: { selected_patterns: [...state.selectedPatterns] } });
        await refreshWatchlist();
      } catch (error) { showToast(error.message, true); }
    }
    closePatternDrawer();
    await scanCurrentPatterns();
  }

  async function scanCurrentPatterns() {
    if (!state.currentInstrument || !state.bars.length || state.scanning) return;
    state.scanning = true;
    dom.scanButton.disabled = true;
    try {
      const payload = await api('/api/patterns/scan', {
        method: 'POST',
        body: {
          instrument_id: state.currentInstrument.id,
          interval: state.interval,
          range: state.range,
          patterns: [...state.selectedPatterns],
          min_confidence: Number(dom.confidenceSlider.value) / 100,
          confirmed_only: dom.confirmedOnly.checked,
          max_bars: Math.max(5000, Number(state.bootstrap.config.max_pattern_bars || 5000)),
        },
      });
      state.events = payload.events || [];
      state.performance = payload.performance || null;
      if (state.performance && Array.isArray(state.performance.horizons) && !state.performance.horizons.includes(state.performanceHorizon)) {
        state.performanceHorizon = Number(state.performance.default_horizon || state.performance.horizons[0] || 20);
      }
      state.activeEventId = null;
      renderPatternResults();
      renderPerformanceStatistics();
      setPatternMarkers();
      drawPatternOverlay();
      const latest = state.events[0];
      dom.patternSummary.textContent = latest ? `最新：${latest.name} ${Math.round(latest.confidence * 100)}%${latest.confirmed ? ' · 已确认' : ''}` : '当前范围未检测到所选形态';
      if (payload.warning) showToast(payload.warning, true);
    } catch (error) {
      showToast(error.message, true);
      state.events = [];
      state.performance = null;
      renderPatternResults();
      renderPerformanceStatistics();
      drawPatternOverlay();
    } finally {
      state.scanning = false;
      dom.scanButton.disabled = false;
    }
  }

  function renderPatternResults() {
    if (!state.selectedPatterns.size) {
      dom.patternResults.className = 'pattern-results empty-state';
      dom.patternResults.textContent = '尚未选择图表形态。';
      return;
    }
    if (!state.events.length) {
      dom.patternResults.className = 'pattern-results empty-state';
      dom.patternResults.textContent = '当前数据范围没有达到阈值的图表形态。';
      return;
    }
    dom.patternResults.className = 'pattern-results';
    dom.patternResults.innerHTML = state.events.map(event => `
      <article class="pattern-result ${state.activeEventId === event.id ? 'active' : ''}" data-event-id="${event.id}">
        <div class="pattern-result-head">
          <span class="pattern-result-name direction-${signalDirection(event)}">${escapeHtml(event.name)}</span>
          <span class="confidence">${Math.round(event.confidence * 100)}%</span>
        </div>
        <div class="pattern-result-meta"><span>${escapeHtml(event.group)}${event.experimental ? ' · 实验算法' : ''}</span><span>${event.confirmed ? '<b class="confirmed-label">已确认</b>' : '形成中'}</span></div>
        <div class="pattern-result-meta"><span>${fmtTime(event.start_time)}</span><span>${fmtTime(event.end_time)}</span></div>
        <div class="pattern-result-meta"><span>${escapeHtml(event.note || '')}</span><span>点击定位</span></div>
      </article>`).join('');
    dom.patternResults.querySelectorAll('[data-event-id]').forEach(element => element.addEventListener('click', () => focusPattern(element.dataset.eventId)));
  }

  function renderPerformanceStatistics() {
    const performance = state.performance;
    if (!performance || !performance.by_horizon) {
      dom.performanceContent.className = 'performance-content empty-state';
      dom.performanceContent.textContent = '完成形态识别后显示历史方向一致率。';
      return;
    }
    const horizons = performance.horizons || [];
    dom.performanceHorizonButtons.querySelectorAll('[data-horizon]').forEach(button => {
      const horizon = Number(button.dataset.horizon);
      button.classList.toggle('active', horizon === state.performanceHorizon);
      button.disabled = !horizons.includes(horizon);
    });
    const data = performance.by_horizon[String(state.performanceHorizon)];
    if (!data) {
      dom.performanceContent.className = 'performance-content empty-state';
      dom.performanceContent.textContent = '当前周期没有可评估的历史样本。';
      return;
    }
    const signals = performance.current_signals || {};
    const bull = signals.bullish || {};
    const bear = signals.bearish || {};
    const neutral = signals.neutral || {};
    const overall = data.overall || {};
    const bias = Number(signals.weighted_net_bias || 0);
    const biasText = bias > 0.12 ? '偏多' : bias < -0.12 ? '偏空' : '多空接近';
    const biasClass = bias > 0.12 ? 'bullish' : bias < -0.12 ? 'bearish' : 'neutral';
    const ciText = overall.win_rate == null ? '--' : `${fmtPct(overall.win_rate_ci_low)} ～ ${fmtPct(overall.win_rate_ci_high)}`;

    const patternRows = (data.by_pattern || []).map(row => {
      const confidenceGap = row.calibration_gap == null ? '--' : fmtSignedPct(row.calibration_gap, 1);
      return `<tr>
        <td><span class="direction-${escapeHtml(row.direction)}">${escapeHtml(row.name)}</span>${row.experimental ? '<small class="experimental-label"> 实验</small>' : ''}</td>
        <td>${row.samples}</td>
        <td><strong>${fmtPct(row.win_rate)}</strong><small>${sampleQualityLabel(row.sample_quality)}</small></td>
        <td>${row.win_rate == null ? '--' : `${fmtPct(row.win_rate_ci_low)}–${fmtPct(row.win_rate_ci_high)}`}</td>
        <td>${fmtSignedPct(row.avg_signed_return)}</td>
        <td>${fmtSignedPct(row.avg_mfe)}</td>
        <td>${fmtSignedPct(row.avg_mae)}</td>
        <td>${fmtPct(row.avg_confidence)}</td>
        <td class="${Number(row.calibration_gap) > 0.08 ? 'warning-text' : ''}">${confidenceGap}</td>
      </tr>`;
    }).join('');

    const bucketRows = (data.confidence_buckets || []).map(row => `<tr>
      <td>${escapeHtml(row.label)}</td>
      <td>${row.samples}</td>
      <td>${fmtPct(row.avg_confidence)}</td>
      <td>${fmtPct(row.win_rate)}</td>
      <td>${row.win_rate == null ? '--' : `${fmtPct(row.win_rate_ci_low)}–${fmtPct(row.win_rate_ci_high)}`}</td>
      <td>${fmtSignedPct(row.calibration_gap, 1)}</td>
    </tr>`).join('');

    dom.performanceContent.className = 'performance-content';
    dom.performanceContent.innerHTML = `
      <div class="signal-stat-grid">
        <article class="signal-stat bullish"><span>看涨样本</span><strong>${bull.count || 0}</strong><small>占比 ${fmtPct(bull.share)} · 平均算法置信 ${fmtPct(bull.avg_confidence)}</small></article>
        <article class="signal-stat bearish"><span>看跌样本</span><strong>${bear.count || 0}</strong><small>占比 ${fmtPct(bear.share)} · 平均算法置信 ${fmtPct(bear.avg_confidence)}</small></article>
        <article class="signal-stat neutral"><span>中性/未突破</span><strong>${neutral.count || 0}</strong><small>占比 ${fmtPct(neutral.share)} · 不计入方向胜率</small></article>
        <article class="signal-stat ${biasClass}"><span>置信度加权偏向</span><strong>${biasText}</strong><small>净偏向 ${fmtSignedPct(bias, 1)}</small></article>
      </div>

      <div class="performance-overview">
        <article><span>${state.performanceHorizon}根K线样本</span><strong>${overall.samples || 0}</strong><small>${sampleQualityLabel(overall.sample_quality)}</small></article>
        <article><span>历史方向一致率</span><strong>${fmtPct(overall.win_rate)}</strong><small>95%区间 ${ciText}</small></article>
        <article><span>平均方向收益</span><strong class="${Number(overall.avg_signed_return) >= 0 ? 'positive-text' : 'negative-text'}">${fmtSignedPct(overall.avg_signed_return)}</strong><small>看跌信号已按做空方向折算</small></article>
        <article><span>平均有利/不利波动</span><strong>${fmtSignedPct(overall.avg_mfe)} / ${fmtSignedPct(overall.avg_mae)}</strong><small>MFE / MAE</small></article>
        <article><span>算法平均置信</span><strong>${fmtPct(overall.avg_confidence)}</strong><small>与实际一致率差 ${fmtSignedPct(overall.calibration_gap, 1)}</small></article>
      </div>

      <div class="performance-table-block">
        <div class="performance-table-title"><strong>各形态历史表现</strong><span>按当前标的、周期、范围、最低置信度和确认条件重新计算</span></div>
        <div class="table-scroll"><table class="performance-table">
          <thead><tr><th>形态</th><th>样本</th><th>方向一致率</th><th>95%区间</th><th>平均方向收益</th><th>MFE</th><th>MAE</th><th>算法置信</th><th>置信差</th></tr></thead>
          <tbody>${patternRows || '<tr><td colspan="9">没有足够的完整未来样本</td></tr>'}</tbody>
        </table></div>
      </div>

      <div class="performance-table-block">
        <div class="performance-table-title"><strong>置信度校准</strong><span>检验算法评分越高时，实际方向一致率是否同步提高</span></div>
        <div class="table-scroll"><table class="performance-table compact">
          <thead><tr><th>算法置信区间</th><th>样本</th><th>平均算法置信</th><th>实际一致率</th><th>95%区间</th><th>置信差</th></tr></thead>
          <tbody>${bucketRows}</tbody>
        </table></div>
      </div>
      <div class="methodology-note"><strong>计算口径：</strong>${escapeHtml((performance.methodology || {}).entry || '')}；${escapeHtml((performance.methodology || {}).success || '')}。<br>${escapeHtml((performance.methodology || {}).warning || '')}</div>
    `;
  }

  function focusPattern(eventId) {
    state.activeEventId = state.activeEventId === eventId ? null : eventId;
    renderPatternResults();
    const event = state.events.find(item => item.id === eventId);
    if (event) {
      const from = event.start_time;
      const to = event.end_time;
      const padding = Math.max(60, (to - from) * 0.35);
      try { chart.api.timeScale().setVisibleRange({ from: from - padding, to: to + padding }); } catch (_) { /* ignore */ }
    }
    drawPatternOverlay();
  }

  function setPatternMarkers() {
    const markers = state.events.slice(0, 60).map(event => ({
      time: Number(event.end_time), position: signalDirection(event) === 'bearish' ? 'aboveBar' : 'belowBar',
      color: DIRECTION_COLORS[signalDirection(event)] || DIRECTION_COLORS.neutral,
      shape: signalDirection(event) === 'bearish' ? 'arrowDown' : signalDirection(event) === 'bullish' ? 'arrowUp' : 'circle',
      text: `${event.name} ${Math.round(event.confidence * 100)}%`,
    })).sort((a, b) => a.time - b.time);
    try {
      if (typeof LightweightCharts.createSeriesMarkers === 'function') {
        if (state.markerPrimitive && typeof state.markerPrimitive.setMarkers === 'function') state.markerPrimitive.setMarkers(markers);
        else state.markerPrimitive = LightweightCharts.createSeriesMarkers(chart.candles, markers, { autoScale: false });
      } else if (typeof chart.candles.setMarkers === 'function') {
        chart.candles.setMarkers(markers);
      }
    } catch (_) { /* marker API differs across versions; SVG overlay still works */ }
  }

  function svgEl(name, attrs = {}) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  }

  function coordinateFor(point) {
    if (!point) return null;
    const x = chart.api.timeScale().timeToCoordinate(Number(point.time));
    const y = chart.candles.priceToCoordinate(Number(point.price));
    if (x == null || y == null || !Number.isFinite(x) || !Number.isFinite(y)) return null;
    return { x, y };
  }

  function drawPatternOverlay() {
    if (!state.chartReady) return;
    resizeOverlay();
    dom.patternOverlay.replaceChildren();
    if (!state.events.length) return;
    const events = state.activeEventId ? state.events.filter(e => e.id === state.activeEventId) : state.events.slice(0, 35);
    events.slice().reverse().forEach(event => {
      const color = DIRECTION_COLORS[signalDirection(event)] || DIRECTION_COLORS.neutral;
      const active = state.activeEventId === event.id;
      const opacity = active ? 1 : Math.max(.25, event.confidence * .7);
      const group = svgEl('g', { opacity });
      (event.lines || []).forEach(segment => {
        const a = coordinateFor(segment.from);
        const b = coordinateFor(segment.to);
        if (!a || !b) return;
        group.appendChild(svgEl('line', {
          x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: color,
          'stroke-width': active ? 2.4 : 1.35,
          'stroke-dasharray': segment.style === 'dashed' ? '6 4' : '0',
          'vector-effect': 'non-scaling-stroke',
        }));
      });
      (event.points || []).forEach(item => {
        const c = coordinateFor(item);
        if (!c) return;
        group.appendChild(svgEl('circle', { cx: c.x, cy: c.y, r: active ? 3.5 : 2.2, fill: '#111722', stroke: color, 'stroke-width': 1.4 }));
        if (item.label && (active || event.experimental)) {
          const text = svgEl('text', { x: c.x + 5, y: c.y - 5, fill: color, 'font-size': 10, 'font-weight': 650 });
          text.textContent = item.label;
          group.appendChild(text);
        }
      });
      const endPoint = coordinateFor((event.points || [])[event.points.length - 1]);
      if (endPoint) {
        const label = svgEl('text', { x: endPoint.x + 7, y: endPoint.y + (signalDirection(event) === 'bearish' ? -10 : 14), fill: color, 'font-size': active ? 12 : 10, 'font-weight': 650 });
        label.textContent = `${event.name} ${Math.round(event.confidence * 100)}%`;
        group.appendChild(label);
      }
      dom.patternOverlay.appendChild(group);
    });
  }

  function renderAlertPatternOptions() {
    const catalog = state.bootstrap.pattern_catalog;
    dom.alertPatternOptions.innerHTML = catalog.map(item => `<label><input type="checkbox" value="${escapeHtml(item.id)}" ${state.selectedPatterns.has(item.id) ? 'checked' : ''}><span class="direction-${item.direction}">${escapeHtml(item.name)}</span></label>`).join('');
  }

  function populateAlertInstruments() {
    dom.alertInstrument.innerHTML = state.watchlist.map(item => `<option value="${escapeHtml(item.instrument_id)}">${escapeHtml(item.symbol)} · ${escapeHtml(marketLabel(item))}</option>`).join('');
    if (state.currentInstrument && state.watchlist.some(item => item.instrument_id === state.currentInstrument.id)) dom.alertInstrument.value = state.currentInstrument.id;
  }

  function openAlertModal() {
    if (!state.watchlist.length) {
      showToast('请先把交易标的加入自选', true);
      return;
    }
    populateAlertInstruments();
    dom.alertInterval.innerHTML = state.bootstrap.intervals.map(v => `<option value="${v}" ${v === state.interval ? 'selected' : ''}>${v.toUpperCase()}</option>`).join('');
    dom.alertName.value = state.currentInstrument ? `${state.currentInstrument.symbol} ${state.interval.toUpperCase()} 图表形态` : '';
    renderAlertPatternOptions();
    dom.alertModal.classList.remove('hidden');
  }

  function closeAlertModal() { dom.alertModal.classList.add('hidden'); }

  async function submitAlert(event) {
    event.preventDefault();
    const patterns = [...dom.alertPatternOptions.querySelectorAll('input:checked')].map(input => input.value);
    if (!patterns.length) {
      showToast('至少选择一个告警形态', true);
      return;
    }
    try {
      const rule = await api('/api/alerts/rules', {
        method: 'POST',
        body: {
          name: dom.alertName.value.trim(), instrument_id: dom.alertInstrument.value,
          interval: dom.alertInterval.value, patterns, match_mode: dom.alertMode.value,
          min_confidence: Number(dom.alertConfidence.value) / 100,
          confirmed_only: dom.alertConfirmed.checked, cooldown_seconds: Number(dom.alertCooldown.value),
          browser_notify: dom.alertBrowser.checked, sound: dom.alertSound.checked,
          lookback_bars: 800, coincidence_bars: 5, enabled: true,
        },
      });
      closeAlertModal();
      await refreshRules();
      showToast(`告警“${rule.name}”已创建`);
      if (rule.browser_notify) requestNotificationPermission();
    } catch (error) { showToast(error.message, true); }
  }

  async function refreshRules() {
    state.rules = await api('/api/alerts/rules');
    renderRules();
  }

  function renderRules() {
    if (!state.rules.length) {
      dom.alertRules.innerHTML = '<div class="empty-state">暂无告警规则</div>';
      return;
    }
    const names = Object.fromEntries(state.bootstrap.pattern_catalog.map(item => [item.id, item.name]));
    dom.alertRules.innerHTML = state.rules.map(rule => `
      <article class="rule-item">
        <div class="rule-head"><div><span class="rule-name">${escapeHtml(rule.name)}</span> <span class="market-tag">${escapeHtml(rule.symbol)}</span></div>
          <div class="rule-actions"><button data-toggle-rule="${rule.id}" data-enabled="${rule.enabled}">${rule.enabled ? '暂停' : '启用'}</button><button data-delete-rule="${rule.id}">删除</button></div></div>
        <div class="rule-meta">${escapeHtml(rule.interval.toUpperCase())} · ${rule.match_mode === 'all' ? '全部满足' : '任一满足'} · 最低 ${Math.round(rule.min_confidence * 100)}% · ${rule.confirmed_only ? '仅已确认' : '形成中也告警'} · 冷却 ${Math.round(rule.cooldown_seconds / 60)}分钟</div>
        <div class="rule-meta">${rule.patterns.map(id => names[id] || id).map(escapeHtml).join('、')}</div>
      </article>`).join('');
    dom.alertRules.querySelectorAll('[data-toggle-rule]').forEach(button => button.addEventListener('click', async () => {
      try {
        await api(`/api/alerts/rules/${button.dataset.toggleRule}`, { method: 'PATCH', body: { enabled: button.dataset.enabled !== 'true' } });
        await refreshRules();
      } catch (error) { showToast(error.message, true); }
    }));
    dom.alertRules.querySelectorAll('[data-delete-rule]').forEach(button => button.addEventListener('click', async () => {
      if (!confirm('确定删除该告警规则？')) return;
      try { await api(`/api/alerts/rules/${button.dataset.deleteRule}`, { method: 'DELETE' }); await refreshRules(); } catch (error) { showToast(error.message, true); }
    }));
  }

  async function pollAlertEvents(initial = false) {
    try {
      const since = initial ? 0 : state.lastAlertId;
      const payload = await api(`/api/alerts/events?since_id=${since}&limit=150`);
      const items = payload.items || [];
      if (items.length) {
        const newest = Math.max(...items.map(item => Number(item.id)));
        if (!initial && newest > state.lastAlertId) {
          items.filter(item => Number(item.id) > state.lastAlertId).reverse().forEach(notifyAlert);
        }
        state.lastAlertId = Math.max(state.lastAlertId, newest);
        localStorage.setItem('gatePattern.lastAlertId', String(state.lastAlertId));
      }
      renderAlertEvents(initial ? items : await fetchRecentEvents());
      updateUnread(payload.unread || 0);
    } catch (_) { /* background polling should stay quiet */ }
  }

  async function fetchRecentEvents() {
    const payload = await api('/api/alerts/events?since_id=0&limit=150');
    updateUnread(payload.unread || 0);
    return payload.items || [];
  }

  function renderAlertEvents(items) {
    if (!items.length) {
      dom.alertEvents.innerHTML = '<div class="empty-state">暂无告警记录</div>';
      return;
    }
    dom.alertEvents.innerHTML = items.map(item => `
      <article class="event-item ${item.is_read ? '' : 'unread'}">
        <div class="event-head"><strong>${escapeHtml(item.rule_name)}</strong><span>${fmtTime(item.triggered_at)}</span></div>
        <div class="event-message">${escapeHtml(item.payload.message || '')}</div>
        <div class="event-meta">${escapeHtml(item.symbol)} · ${escapeHtml(item.interval.toUpperCase())} · ${item.payload.match_mode === 'all' ? '全部满足' : '任一满足'}</div>
      </article>`).join('');
  }

  function updateUnread(count) {
    dom.unreadBadge.textContent = count;
    dom.unreadBadge.classList.toggle('hidden', !count);
  }

  function requestNotificationPermission() {
    if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(() => {});
  }

  function notifyAlert(item) {
    const message = item.payload && item.payload.message ? item.payload.message : `${item.symbol} 检测到图表形态`;
    showToast(message);
    if (item.payload && item.payload.browser_notify && 'Notification' in window && Notification.permission === 'granted') {
      const notification = new Notification('Gate 图表形态告警', { body: message, tag: item.event_key });
      notification.onclick = () => { window.focus(); switchView('alerts-view'); };
    }
    if (item.payload && item.payload.sound) beep();
  }

  function beep() {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioContext();
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.22);
      oscillator.connect(gain).connect(ctx.destination);
      oscillator.start(); oscillator.stop(ctx.currentTime + 0.24);
    } catch (_) { /* ignore audio restrictions */ }
  }


  function durationLabel(seconds) {
    const value = Number(seconds || 0);
    if (!Number.isFinite(value) || value <= 0) return '--';
    if (value >= 86400) return `${(value / 86400).toFixed(value % 86400 ? 1 : 0)}天`;
    if (value >= 3600) return `${(value / 3600).toFixed(value % 3600 ? 1 : 0)}小时`;
    if (value >= 60) return `${(value / 60).toFixed(value % 60 ? 1 : 0)}分钟`;
    return `${Math.round(value)}秒`;
  }

  function checkedValues(container, attribute) {
    return [...container.querySelectorAll('input[type="checkbox"]:checked')].map(input => attribute === 'number' ? Number(input.value) : input.value);
  }

  function setCheckedValues(container, values) {
    const selected = new Set((values || []).map(String));
    container.querySelectorAll('input[type="checkbox"]').forEach(input => { input.checked = selected.has(String(input.value)); });
  }

  function renderOpportunitySettings() {
    const settings = state.opportunitySettings || {};
    const selectedWatch = new Set((settings.watchlist_ids && settings.watchlist_ids.length ? settings.watchlist_ids : state.watchlist.map(row => row.id)).map(Number));
    dom.opWatchlistOptions.innerHTML = state.watchlist.length ? state.watchlist.map(item => `
      <label class="op-check-item"><input type="checkbox" value="${item.id}" ${selectedWatch.has(Number(item.id)) ? 'checked' : ''}>
        <span><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(marketLabel(item))}</small></span>
      </label>`).join('') : '<div class="empty-state">请先在行情页加入自选</div>';

    const selectedIntervals = new Set(settings.intervals || ['15m', '1h', '4h', '1d']);
    dom.opIntervalOptions.innerHTML = (state.bootstrap.intervals || []).map(interval => `
      <label class="op-chip ${selectedIntervals.has(interval) ? 'checked' : ''}"><input type="checkbox" value="${interval}" ${selectedIntervals.has(interval) ? 'checked' : ''}><span>${interval.toUpperCase()}</span></label>`).join('');

    const selectedPatterns = new Set(settings.patterns && settings.patterns.length ? settings.patterns : state.bootstrap.pattern_catalog.map(row => row.id));
    dom.opPatternOptions.innerHTML = state.bootstrap.pattern_catalog.map(item => `
      <label class="op-pattern-item"><input type="checkbox" value="${escapeHtml(item.id)}" ${selectedPatterns.has(item.id) ? 'checked' : ''}>
        <span class="direction-${item.direction}">${escapeHtml(item.name)}</span><small>${escapeHtml(item.group)}</small>
      </label>`).join('');

    dom.opMinConfidence.value = Math.round(Number(settings.min_confidence ?? .60) * 100);
    dom.opMinWinRate.value = Math.round(Number(settings.min_win_rate ?? .55) * 100);
    dom.opMinSamples.value = Number(settings.min_samples ?? 12);
    dom.opActiveBars.value = Number(settings.active_bars ?? 5);
    dom.opMaxBars.value = String(settings.max_bars ?? 5000);
    [...dom.opHorizons.options].forEach(option => { option.selected = (settings.horizons || [5, 10, 20, 50]).map(Number).includes(Number(option.value)); });
    dom.opConfirmedOnly.checked = Boolean(settings.confirmed_only);
    dom.opAutoEnabled.checked = Boolean(settings.auto_enabled);
    dom.opAutoSeconds.value = String(settings.auto_seconds ?? 900);
    updateOpportunitySelectionCounts();
    bindOpportunityDynamicChecks();
  }

  function bindOpportunityDynamicChecks() {
    dom.opIntervalOptions.querySelectorAll('input').forEach(input => input.addEventListener('change', () => {
      input.closest('.op-chip').classList.toggle('checked', input.checked);
      updateOpportunitySelectionCounts();
    }));
    dom.opWatchlistOptions.querySelectorAll('input').forEach(input => input.addEventListener('change', updateOpportunitySelectionCounts));
    dom.opPatternOptions.querySelectorAll('input').forEach(input => input.addEventListener('change', updateOpportunitySelectionCounts));
  }

  function updateOpportunitySelectionCounts() {
    const watchCount = dom.opWatchlistOptions.querySelectorAll('input:checked').length;
    const patternCount = dom.opPatternOptions.querySelectorAll('input:checked').length;
    dom.opWatchSelected.textContent = `${watchCount}个`;
    dom.opPatternSelected.textContent = patternCount === state.bootstrap.pattern_catalog.length ? '全部' : `${patternCount}个`;
    dom.opMinConfidenceValue.textContent = `${dom.opMinConfidence.value}%`;
    dom.opMinWinRateValue.textContent = `${dom.opMinWinRate.value}%`;
  }

  function collectOpportunitySettings() {
    return {
      watchlist_ids: checkedValues(dom.opWatchlistOptions, 'number'),
      intervals: checkedValues(dom.opIntervalOptions, 'string'),
      patterns: checkedValues(dom.opPatternOptions, 'string'),
      min_confidence: Number(dom.opMinConfidence.value) / 100,
      min_win_rate: Number(dom.opMinWinRate.value) / 100,
      min_samples: Number(dom.opMinSamples.value),
      confirmed_only: dom.opConfirmedOnly.checked,
      active_bars: Number(dom.opActiveBars.value),
      max_bars: Number(dom.opMaxBars.value),
      horizons: [...dom.opHorizons.selectedOptions].map(option => Number(option.value)),
      auto_enabled: dom.opAutoEnabled.checked,
      auto_seconds: Number(dom.opAutoSeconds.value),
    };
  }

  async function saveOpportunitySettings(showMessage = true) {
    const payload = collectOpportunitySettings();
    if (!payload.watchlist_ids.length) throw new Error('至少选择一个自选标的');
    if (!payload.intervals.length) throw new Error('至少选择一个时间周期');
    if (!payload.patterns.length) throw new Error('至少选择一个图表形态');
    if (!payload.horizons.length) throw new Error('至少选择一个回测持有周期');
    state.opportunitySettings = await api('/api/opportunities/settings', { method: 'PUT', body: payload });
    if (showMessage) showToast('机会扫描设置已保存');
    return state.opportunitySettings;
  }

  async function startOpportunityRun() {
    dom.opRun.disabled = true;
    try {
      await saveOpportunitySettings(false);
      await api('/api/opportunities/run', { method: 'POST' });
      showToast('已开始遍历自选、周期与图表形态');
      beginOpportunityPolling();
    } catch (error) {
      showToast(error.message, true);
      dom.opRun.disabled = false;
    }
  }

  function beginOpportunityPolling() {
    clearInterval(state.opportunityPollTimer);
    pollOpportunityStatus();
    state.opportunityPollTimer = setInterval(pollOpportunityStatus, 1800);
  }

  async function pollOpportunityStatus() {
    try {
      const payload = await api('/api/opportunities/status');
      const status = payload.status || {};
      const run = payload.latest_run || null;
      const total = Number(status.total_tasks || (run && run.total_tasks) || 0);
      const completed = Number(status.completed_tasks || (run && run.completed_tasks) || 0);
      const running = Boolean(status.running || (run && run.status === 'running'));
      dom.opRun.disabled = running;
      dom.opRunTitle.textContent = running ? `正在扫描：${status.current_label || '准备中'}` : run ? `第 ${run.id} 次扫描 · ${run.status === 'completed' ? '已完成' : run.status}` : '尚未执行扫描';
      dom.opRunDetail.textContent = run ? `开始 ${fmtTime(run.started_at)}${run.finished_at ? ` · 完成 ${fmtTime(run.finished_at)}` : ''}${run.error ? ` · ${run.error}` : ''}` : '选择自选和周期后，点击“开始遍历回测”。';
      const ratio = total ? Math.min(100, completed / total * 100) : 0;
      dom.opProgressBar.style.width = `${ratio}%`;
      dom.opProgressText.textContent = `${completed}/${total}`;
      if (!running) {
        clearInterval(state.opportunityPollTimer);
        state.opportunityPollTimer = null;
        await loadOpportunityResults();
      }
    } catch (error) {
      dom.opRunDetail.textContent = error.message;
    }
  }

  async function loadOpportunityResults() {
    state.opportunityData = await api('/api/opportunities/results');
    renderOpportunitySummary();
    renderOpportunityResults();
  }

  function renderOpportunitySummary() {
    const summary = state.opportunityData && state.opportunityData.summary || {};
    const values = [summary.tasks || 0, summary.actionable || 0, summary.bullish || 0, summary.bearish || 0, summary.no_signal || 0];
    [...dom.opSummaryGrid.querySelectorAll('strong')].forEach((node, index) => { node.textContent = values[index] ?? 0; });
  }

  function opportunityStatusLabel(task, signal) {
    if (task.status === 'error') return ['异常', 'error'];
    if (task.actionable_count > 0) return [signal && signal.direction === 'bearish' ? '可做空' : '可做多', 'actionable'];
    if (task.status === 'watch') return ['观察', 'watch'];
    return ['无信号', 'no-signal'];
  }

  function filteredOpportunityTasks() {
    let tasks = [...((state.opportunityData && state.opportunityData.tasks) || [])];
    const statusFilter = dom.opStatusFilter.value;
    const directionFilter = dom.opDirectionFilter.value;
    if (statusFilter === 'actionable') tasks = tasks.filter(row => Number(row.actionable_count) > 0);
    else if (statusFilter !== 'all') tasks = tasks.filter(row => row.status === statusFilter);
    if (directionFilter !== 'all') tasks = tasks.filter(row => row.best_signal && row.best_signal.direction === directionFilter);
    const sort = dom.opSort.value;
    tasks.sort((a, b) => {
      const sa = a.best_signal || {};
      const sb = b.best_signal || {};
      if (sort === 'symbol') return String(a.symbol).localeCompare(String(b.symbol));
      if (sort === 'win_rate') return Number(sb.win_rate || -1) - Number(sa.win_rate || -1);
      if (sort === 'confidence') return Number(sb.confidence || -1) - Number(sa.confidence || -1);
      return Number(sb.score || -1) - Number(sa.score || -1);
    });
    return tasks;
  }

  function signalDetailsHtml(signals) {
    if (!signals || signals.length <= 1) return '';
    return `<details class="op-signal-details"><summary>另外 ${signals.length - 1} 个当前形态</summary>${signals.slice(1).map(signal => `
      <div><span class="direction-${signal.direction}">${escapeHtml(signal.name)}</span> · 可信度 ${fmtPct(signal.confidence)} · 胜率 ${fmtPct(signal.win_rate)} · ${signal.samples}样本 · ${escapeHtml(signal.holding_label || durationLabel(signal.holding_seconds))}</div>`).join('')}</details>`;
  }

  function renderOpportunityResults() {
    const tasks = filteredOpportunityTasks();
    if (!tasks.length) {
      dom.opResultsBody.innerHTML = '<tr><td colspan="12" class="empty-cell">当前筛选条件下暂无结果</td></tr>';
      return;
    }
    dom.opResultsBody.innerHTML = tasks.map(task => {
      const signal = task.best_signal;
      const [label, cls] = opportunityStatusLabel(task, signal);
      const direction = signal ? signal.direction : 'neutral';
      const winTitle = signal && signal.win_rate != null ? `95%区间 ${fmtPct(signal.ci_low)} - ${fmtPct(signal.ci_high)}；平均方向收益 ${fmtSignedPct(signal.avg_signed_return)}` : '';
      return `<tr class="op-row ${task.actionable_count ? 'has-opportunity' : ''}">
        <td><strong>${escapeHtml(task.symbol)}</strong><small>${escapeHtml(marketLabel(task))}</small></td>
        <td>${escapeHtml(task.interval.toUpperCase())}<small>${task.candles_count || 0}根历史</small></td>
        <td><span class="op-status ${cls}">${label}</span>${task.error ? `<small class="negative-text">${escapeHtml(task.error)}</small>` : ''}</td>
        <td>${signal ? `<span class="direction-${direction}">${direction === 'bullish' ? '看涨' : '看跌'} · ${escapeHtml(signal.name)}</span>${signalDetailsHtml(task.signals)}` : '--'}</td>
        <td>${signal ? `<strong>${fmtPct(signal.confidence)}</strong><small>${signal.confirmed ? '已确认' : '未确认'}</small>` : '--'}</td>
        <td title="${escapeHtml(winTitle)}">${signal && signal.win_rate != null ? `<strong>${fmtPct(signal.win_rate)}</strong><small>${fmtPct(signal.ci_low)}~${fmtPct(signal.ci_high)}</small>` : '--'}</td>
        <td>${signal ? `${signal.samples || 0}<small>${signal.wins || 0}次方向一致</small>` : '--'}</td>
        <td>${signal ? `${escapeHtml(signal.holding_label || durationLabel(signal.holding_seconds))}<small>${signal.recommended_horizon || '--'}根K线</small>` : '--'}</td>
        <td>${signal ? `${escapeHtml(signal.duration_label || durationLabel(signal.duration_seconds))}<small>${signal.duration_bars || 0}根</small>` : '--'}</td>
        <td>${signal ? `${signal.age_bars || 0}根前<small>${fmtTime(signal.end_time)}</small>` : '--'}</td>
        <td>${signal ? `<strong>${fmtPct(signal.score)}</strong><small>历史质量 ${fmtPct(signal.history_quality)}</small>` : '--'}</td>
        <td>${signal ? `<button type="button" class="op-view-chart" data-instrument="${escapeHtml(task.instrument_id)}" data-interval="${escapeHtml(task.interval)}" data-pattern="${escapeHtml(signal.pattern)}">看图</button>` : '--'}</td>
      </tr>`;
    }).join('');
    dom.opResultsBody.querySelectorAll('.op-view-chart').forEach(button => button.addEventListener('click', () => openOpportunityChart(button.dataset.instrument, button.dataset.interval, button.dataset.pattern)));
  }

  async function openOpportunityChart(instrumentId, interval, patternId) {
    const item = state.watchlist.find(row => row.instrument_id === instrumentId);
    if (!item) return showToast('该标的已不在自选中', true);
    state.currentWatch = item;
    state.currentInstrument = {
      id: item.instrument_id, market: item.market, settle: item.settle, symbol: item.symbol,
      display_name: item.display_name, base: item.base, quote: item.quote, is_stock: item.is_stock,
    };
    state.interval = interval;
    if (patternId) {
      state.selectedPatterns = new Set([patternId]);
      persistPatterns();
      renderPatternOptions();
    }
    localStorage.setItem('gatePattern.interval', interval);
    updateToolbarButtons();
    updateCurrentHeader();
    renderWatchlist();
    switchView('market-view');
    await loadCurrentMarket(true);
  }

  function switchView(id) {
    document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === id));
    document.querySelectorAll('.top-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.view === id));
    if (id === 'alerts-view') {
      refreshRules();
      fetchRecentEvents().then(renderAlertEvents).catch(() => {});
    } else if (id === 'opportunities-view') {
      renderOpportunitySettings();
      loadOpportunityResults().catch(error => showToast(error.message, true));
      pollOpportunityStatus();
    } else if (state.chartReady) {
      setTimeout(() => { chart.api.applyOptions({ width: dom.mainChart.clientWidth, height: dom.mainChart.clientHeight }); drawPatternOverlay(); }, 30);
    }
  }

  function switchSide(id) {
    document.querySelectorAll('.side-panel').forEach(panel => panel.classList.toggle('active', panel.id === id));
    document.querySelectorAll('.sidebar-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.side === id));
  }

  async function refreshStatus() {
    try {
      const payload = await api('/api/status');
      const scanner = payload.scanner || {};
      dom.scannerDot.className = `status-dot ${scanner.last_error ? 'error' : scanner.running ? 'online' : ''}`;
      dom.scannerText.textContent = scanner.last_error ? `异常：${scanner.last_error}` : scanner.running ? `后台扫描中 · ${payload.watchlist_count}自选` : '扫描器已停止';
      updateUnread(payload.unread_alerts || 0);
    } catch (_) {
      dom.scannerDot.className = 'status-dot error';
      dom.scannerText.textContent = '后端不可用';
    }
  }

  function bindEvents() {
    document.querySelectorAll('.top-tab').forEach(tab => tab.addEventListener('click', () => switchView(tab.dataset.view)));
    document.querySelectorAll('.sidebar-tab').forEach(tab => tab.addEventListener('click', () => switchSide(tab.dataset.side)));
    dom.instrumentSearch.addEventListener('input', debounceSearch);
    dom.marketFilters.querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
      state.marketFilter = button.dataset.market;
      dom.marketFilters.querySelectorAll('button').forEach(item => item.classList.toggle('active', item === button));
      searchInstruments();
    }));
    dom.catalogRefresh.addEventListener('click', async () => {
      dom.catalogRefresh.disabled = true;
      try { const payload = await api('/api/instruments/refresh', { method: 'POST' }); showToast(`已刷新 ${payload.updated} 个交易标的`); await searchInstruments(); }
      catch (error) { showToast(error.message, true); }
      finally { dom.catalogRefresh.disabled = false; }
    });
    dom.refreshCurrent.addEventListener('click', () => loadCurrentMarket(false));
    dom.wakeScanner.addEventListener('click', async () => { showToast('后台扫描器将在下一轮立即更新'); await loadCurrentMarket(false); });
    dom.performanceHorizonButtons.querySelectorAll('[data-horizon]').forEach(button => button.addEventListener('click', () => {
      const horizon = Number(button.dataset.horizon);
      if (button.disabled || horizon === state.performanceHorizon) return;
      state.performanceHorizon = horizon;
      localStorage.setItem('gatePattern.performanceHorizon', String(horizon));
      renderPerformanceStatistics();
    }));
    dom.patternButton.addEventListener('click', openPatternDrawer);
    document.querySelectorAll('[data-close-drawer]').forEach(item => item.addEventListener('click', closePatternDrawer));
    dom.patternSearch.addEventListener('input', () => renderPatternOptions(dom.patternSearch.value));
    dom.selectAllPatterns.addEventListener('click', () => { state.bootstrap.pattern_catalog.forEach(item => state.selectedPatterns.add(item.id)); renderPatternOptions(dom.patternSearch.value); });
    dom.clearPatterns.addEventListener('click', () => { state.selectedPatterns.clear(); renderPatternOptions(dom.patternSearch.value); });
    dom.applyPatterns.addEventListener('click', applyPatterns);
    dom.scanButton.addEventListener('click', scanCurrentPatterns);
    dom.confidenceSlider.addEventListener('input', () => { dom.confidenceValue.textContent = `${dom.confidenceSlider.value}%`; });
    dom.confidenceSlider.addEventListener('change', scanCurrentPatterns);
    dom.confirmedOnly.addEventListener('change', scanCurrentPatterns);
    dom.resetChart.addEventListener('click', resetChart);
    dom.exportCsv.addEventListener('click', () => {
      if (!state.currentInstrument) return showToast('请先选择交易标的', true);
      window.open(`/api/candles/export?instrument_id=${encodeURIComponent(state.currentInstrument.id)}&interval=${state.interval}&range=${state.range}`, '_blank');
    });
    dom.newAlert.addEventListener('click', openAlertModal);
    document.querySelectorAll('[data-close-alert]').forEach(item => item.addEventListener('click', closeAlertModal));
    dom.alertForm.addEventListener('submit', submitAlert);
    dom.alertConfidence.addEventListener('input', () => { dom.alertConfidenceValue.textContent = `${dom.alertConfidence.value}%`; });
    dom.markAllRead.addEventListener('click', async () => { await api('/api/alerts/events/read', { method: 'POST', body: { ids: [] } }); updateUnread(0); renderAlertEvents(await fetchRecentEvents()); });
    dom.opWatchAll.addEventListener('click', () => { dom.opWatchlistOptions.querySelectorAll('input').forEach(input => { input.checked = true; }); updateOpportunitySelectionCounts(); });
    dom.opWatchClear.addEventListener('click', () => { dom.opWatchlistOptions.querySelectorAll('input').forEach(input => { input.checked = false; }); updateOpportunitySelectionCounts(); });
    dom.opPatternAll.addEventListener('click', () => { dom.opPatternOptions.querySelectorAll('input').forEach(input => { input.checked = true; }); updateOpportunitySelectionCounts(); });
    dom.opPatternClear.addEventListener('click', () => { dom.opPatternOptions.querySelectorAll('input').forEach(input => { input.checked = false; }); updateOpportunitySelectionCounts(); });
    dom.opMinConfidence.addEventListener('input', updateOpportunitySelectionCounts);
    dom.opMinWinRate.addEventListener('input', updateOpportunitySelectionCounts);
    dom.opSaveSettings.addEventListener('click', () => saveOpportunitySettings(true).catch(error => showToast(error.message, true)));
    dom.opRun.addEventListener('click', startOpportunityRun);
    dom.opRefreshResults.addEventListener('click', () => loadOpportunityResults().catch(error => showToast(error.message, true)));
    dom.opStatusFilter.addEventListener('change', renderOpportunityResults);
    dom.opDirectionFilter.addEventListener('change', renderOpportunityResults);
    dom.opSort.addEventListener('change', renderOpportunityResults);
  }

  function cacheDom() {
    [
      'toast', 'scanner-dot', 'scanner-text', 'refresh-current', 'unread-badge', 'watch-count', 'instrument-search',
      'catalog-refresh', 'market-filters', 'instrument-results', 'watchlist', 'wake-scanner', 'current-symbol',
      'current-market', 'stock-pill', 'current-name', 'last-price', 'last-time', 'interval-buttons', 'range-buttons',
      'pattern-button', 'selected-pattern-count', 'scan-button', 'reset-chart', 'export-csv', 'ohlc-legend',
      'pattern-summary', 'chart-container', 'main-chart', 'pattern-overlay', 'chart-empty', 'chart-loading', 'data-status',
      'confidence-value', 'confidence-slider', 'confirmed-only', 'pattern-results', 'performance-horizon-buttons',
      'performance-content', 'pattern-drawer', 'pattern-search',
      'select-all-patterns', 'clear-patterns', 'pattern-options', 'drawer-selected-text', 'apply-patterns', 'alert-modal',
      'alert-form', 'alert-name', 'alert-instrument', 'alert-interval', 'alert-mode', 'alert-cooldown', 'alert-confidence',
      'alert-confidence-value', 'alert-confirmed', 'alert-browser', 'alert-sound', 'alert-pattern-options', 'new-alert',
      'alert-rules', 'alert-events', 'mark-all-read', 'op-watch-selected', 'op-watchlist-options', 'op-watch-all',
      'op-watch-clear', 'op-interval-options', 'op-pattern-selected', 'op-pattern-options', 'op-pattern-all',
      'op-pattern-clear', 'op-min-confidence', 'op-min-confidence-value', 'op-min-win-rate', 'op-min-win-rate-value',
      'op-min-samples', 'op-active-bars', 'op-max-bars', 'op-horizons', 'op-confirmed-only', 'op-auto-enabled',
      'op-auto-seconds', 'op-save-settings', 'op-run', 'op-run-title', 'op-run-detail', 'op-progress-bar',
      'op-progress-text', 'op-summary-grid', 'op-status-filter', 'op-direction-filter', 'op-sort',
      'op-refresh-results', 'op-results-body'
    ].forEach(id => { dom[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = $(id); });
  }

  async function init() {
    cacheDom();
    bindEvents();
    try {
      state.bootstrap = await api('/api/bootstrap');
      if (!state.bootstrap.instrument_count) showToast('Gate 市场目录正在后台初始化，请稍后点击刷新');
      state.watchlist = state.bootstrap.watchlist || [];
      state.rules = state.bootstrap.alert_rules || [];
      state.opportunitySettings = state.bootstrap.opportunity && state.bootstrap.opportunity.settings || null;
      if (!state.bootstrap.intervals.includes(state.interval)) state.interval = state.bootstrap.config.default_interval;
      if (!state.bootstrap.ranges.includes(state.range)) state.range = state.bootstrap.config.default_range;
      createToolbarButtons();
      renderPatternOptions();
      renderPerformanceStatistics();
      renderWatchlist();
      renderOpportunitySettings();
      renderRules();
      populateAlertInstruments();
      updateUnread(state.bootstrap.unread_alerts || 0);
      await initChart();
      await searchInstruments();
      if (state.watchlist.length) await selectWatch(state.watchlist[0]);
      pollAlertEvents(true);
      state.alertPollTimer = setInterval(() => pollAlertEvents(false), 8000);
      setInterval(refreshStatus, 10000);
      refreshStatus();
      loadOpportunityResults().catch(() => {});
    } catch (error) {
      showToast(error.message, true);
      dom.scannerText.textContent = error.message;
      dom.scannerDot.className = 'status-dot error';
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
