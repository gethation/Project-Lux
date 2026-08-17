/* Project Lux — spread chart.
 *
 * Two panes sharing one time axis:
 *   pane 0  spread candles + the executable band (long / short) + the rolling
 *           mean ± entry_z·std thresholds, all in spread units.
 *   pane 1  long_z / short_z, where the thresholds are flat lines and the
 *           thickness of the band IS the execution cost in sigma.
 *
 * long_spread >= mid >= short_spread always holds (short quotes UMC at the bid
 * and CCF at the ask, long the other way -- tradable_spread.py:112), so the two
 * band lines never cross and are drawn pale and dashed in both panes. What an
 * entry has to clear is the NEAR edge, not the middle.
 */

const CDN = "https://unpkg.com/lightweight-charts@5/dist/lightweight-charts.standalone.production.js";

const COLORS = {
  up: "#26a69a",
  down: "#ef5350",
  // The band is context, not a signal to stare at: pale dashed in both panes,
  // so the candles and the z thresholds stay the things that read first.
  long: "#93b3ff",
  short: "#ffbd85",
  mean: "#787b86",
  entryUpper: "#ef5350",
  entryLower: "#26a69a",
  exit: "#b2b5be",
  grid: "#f0f3fa",
  border: "#e0e3eb",
  text: "#131722",
  muted: "#787b86",
};

const STATE_LABEL = {
  flat: "FLAT",
  entry_pending: "ENTRY PENDING",
  open: "OPEN",
  exit_pending: "EXIT PENDING",
  paused: "PAUSED",
  error: "ERROR",
  forced_closed_end_of_data: "FORCED CLOSED",
};

const state = {
  chart: null,
  interval: "1m",
  showBand: true,
  showThresholds: true,
  showMarkers: true,
  userMovedChart: false,
  chartData: null,
  meta: null,
  live: null,
  priceLines: [],
};

// ----------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = resolve;
    el.onerror = () => reject(new Error(src));
    document.head.appendChild(el);
  });
}

async function ensureLibrary() {
  if (window.LightweightCharts) return window.LightweightCharts;
  await loadScript(CDN);
  if (!window.LightweightCharts) throw new Error("lightweight-charts failed to load");
  return window.LightweightCharts;
}

const num = (value, digits = 2) =>
  value === null || value === undefined || Number.isNaN(value)
    ? "—"
    : Number(value).toFixed(digits);

const money = (value) =>
  value === null || value === undefined
    ? "—"
    : (value >= 0 ? "+" : "−") +
      Math.abs(value).toLocaleString("en-US", { maximumFractionDigits: 0 });

/* Times from the API are already shifted so that rendering them as UTC shows
   Taipei wall clock (queries.py:display_epoch). Format them the same way. */
function clock(epoch) {
  if (epoch === null || epoch === undefined) return "—";
  const d = new Date(epoch * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCMonth() + 1)}/${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

function age(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

const lineData = (rows, key) =>
  rows.map((row) =>
    row[key] === null || row[key] === undefined
      ? { time: row.time }
      : { time: row.time, value: row[key] }
  );

async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

// ------------------------------------------------------------------- chart
async function main() {
  let LWC;
  try {
    LWC = await ensureLibrary();
  } catch (error) {
    fatal(
      "Could not load lightweight-charts. This machine cannot reach the CDN — vendor the standalone bundle instead:",
      "curl -o lux_trader/web/static/lightweight-charts.standalone.production.js \\\n" +
        `  ${CDN}`
    );
    return;
  }

  const {
    createChart,
    CandlestickSeries,
    LineSeries,
    createSeriesMarkers,
    LineStyle,
    CrosshairMode,
  } = LWC;

  const container = $("chart");
  const chart = createChart(container, {
    autoSize: true,
    width: container.clientWidth,
    height: container.clientHeight,
    layout: {
      background: { color: "#ffffff" },
      textColor: COLORS.text,
      fontFamily: '"Trebuchet MS", Roboto, Ubuntu, sans-serif',
      fontSize: 11,
    },
    // Without this the time axis follows the browser locale, which on this
    // machine renders the dates in Chinese while the rest of the UI is English.
    localization: { locale: "en-US" },
    grid: {
      vertLines: { color: COLORS.grid },
      horzLines: { color: COLORS.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: {
        color: "#9598a1",
        width: 1,
        style: LineStyle.Dashed,
        labelBackgroundColor: COLORS.text,
      },
      horzLine: {
        color: "#9598a1",
        width: 1,
        style: LineStyle.Dashed,
        labelBackgroundColor: COLORS.text,
      },
    },
    rightPriceScale: {
      borderColor: COLORS.border,
      scaleMargins: { top: 0.12, bottom: 0.12 },
    },
    timeScale: {
      borderColor: COLORS.border,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 4,
    },
  });

  // ---- pane 0: spread -------------------------------------------------
  const thin = {
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  };
  const candles = chart.addSeries(CandlestickSeries, {
    upColor: COLORS.up,
    downColor: COLORS.down,
    borderUpColor: COLORS.up,
    borderDownColor: COLORS.down,
    wickUpColor: COLORS.up,
    wickDownColor: COLORS.down,
    priceFormat: { type: "price", precision: 3, minMove: 0.001 },
    // Suppressed because renderPriceLines draws the same level itself, and
    // that one carries the unrealized P&L in its title.
    priceLineVisible: false,
  });
  const longLine = chart.addSeries(LineSeries, {
    ...thin,
    color: COLORS.long,
    lineStyle: LineStyle.Dashed,
  });
  const shortLine = chart.addSeries(LineSeries, {
    ...thin,
    color: COLORS.short,
    lineStyle: LineStyle.Dashed,
  });
  const meanLine = chart.addSeries(LineSeries, {
    ...thin,
    color: COLORS.mean,
    lineStyle: LineStyle.Dotted,
  });
  const entryUpper = chart.addSeries(LineSeries, {
    ...thin,
    color: COLORS.entryUpper,
    lineStyle: LineStyle.Dashed,
  });
  const entryLower = chart.addSeries(LineSeries, {
    ...thin,
    color: COLORS.entryLower,
    lineStyle: LineStyle.Dashed,
  });

  const markers = createSeriesMarkers(candles, []);

  // ---- pane 1: z -------------------------------------------------------
  // All three z lines share one weight and one dotted pattern; only the colour
  // separates them. The lines that should read first in this pane are the flat
  // thresholds below, not the z series wandering between them.
  const zOptions = {
    lineWidth: 1,
    lineStyle: LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: true,
    crosshairMarkerVisible: false,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  };
  const longZ = chart.addSeries(LineSeries, { ...zOptions, color: COLORS.long }, 1);
  const shortZ = chart.addSeries(LineSeries, { ...zOptions, color: COLORS.short }, 1);
  // The mid z is what replay records and what the store has during warmup; a
  // store with no order book (replay has none) would otherwise show an empty pane.
  const midZ = chart.addSeries(LineSeries, { ...zOptions, color: COLORS.mean }, 1);

  /* The z thresholds are constant SERIES, not price lines. Two reasons:
     a price line hangs off one series and renders nothing when that series has
     no data (replay stores carry no order book, so long_z/short_z are all
     null), and price lines are excluded from autoscale -- so the pane would
     happily zoom in until the threshold you are waiting for is off-screen. */
  const constant = (color, style) =>
    chart.addSeries(
      LineSeries,
      {
        color,
        lineWidth: 1,
        lineStyle: style,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      },
      1
    );
  const zThresholds = {
    entryUpper: constant(COLORS.entryUpper, LineStyle.Dashed),
    entryLower: constant(COLORS.entryLower, LineStyle.Dashed),
    exitUpper: constant(COLORS.exit, LineStyle.Dotted),
    exitLower: constant(COLORS.exit, LineStyle.Dotted),
  };

  const series = {
    chart,
    candles,
    longLine,
    shortLine,
    meanLine,
    entryUpper,
    entryLower,
    longZ,
    shortZ,
    midZ,
    zThresholds,
    markers,
    container,
  };

  state.chart = chart;
  wireToolbar(series);
  wireLegend(series);
  wireAutoFit(series);
  watchPaneDivider();

  state.meta = await api("/api/meta");
  if (state.meta.default_interval) state.interval = state.meta.default_interval;
  applyMeta(state.meta);

  await refreshChart(series, true);
  await refreshLive(series);

  setInterval(() => refreshChart(series, false).catch(reportError), 30000);
  setInterval(() => refreshLive(series).catch(reportError), 3000);
  return series;
}

// ------------------------------------------------------------------ layers
async function refreshChart(series, fit) {
  const data = await api(`/api/chart?interval=${state.interval}&limit=1500`);
  const previousCount = state.chartData?.candles.length ?? 0;
  const previousRange = series.chart.timeScale().getVisibleLogicalRange();
  state.chartData = data;

  const band = state.showBand ? data.band : [];
  const thresholds = state.showThresholds ? data.thresholds : [];

  series.candles.setData(data.candles);
  series.longLine.setData(lineData(band, "long"));
  series.shortLine.setData(lineData(band, "short"));
  series.meanLine.setData(lineData(thresholds, "mean"));
  series.entryUpper.setData(lineData(thresholds, "entry_upper"));
  series.entryLower.setData(lineData(thresholds, "entry_lower"));
  series.longZ.setData(lineData(data.z, "long_z"));
  series.shortZ.setData(lineData(data.z, "short_z"));
  series.midZ.setData(lineData(data.z, "mid_z"));
  setZThresholds(series, data.z);

  series.markers.setMarkers(state.showMarkers ? data.markers.map(markerStyle) : []);

  $("legendInterval").textContent = data.interval;
  $("pSource").textContent = data.intrabar ? "ticks → O/H/L, bars → C" : "bars only";
  $("pCounts").textContent = `${state.meta.summary.bars} / ${state.meta.summary.trades}`;
  updateLegend(series, null);

  /* setData keeps the bar spacing rather than the range, so every 30s poll
     used to shove the view a few hundred slots to the right. Re-fit when the
     operator has not taken control of the chart; otherwise put their range
     back, shifted by however many candles arrived. */
  if (fit || !state.userMovedChart) {
    fitLater(series);
  } else if (previousRange) {
    const shift = data.candles.length - previousCount;
    series.chart.timeScale().setVisibleLogicalRange({
      from: previousRange.from + shift,
      to: previousRange.to + shift,
    });
  }
}

/* autoSize measures asynchronously, so a fitContent() issued in the same frame
   as the first setData sizes the bars for a container that is still 0px wide --
   and the data ends up crammed against one edge. Fit once the layout has
   actually settled instead. */
function fitLater(series) {
  state.userMovedChart = false;
  // Immediately, so a single rendered frame is already correct; then again once
  // layout has settled, because autoSize measures asynchronously and the first
  // call can be sized against a container that is still 0px wide.
  applyFit(series);
  requestAnimationFrame(() => requestAnimationFrame(() => applyFit(series)));
  setTimeout(() => applyFit(series), 300);
}

/* fitContent() applies on a later render tick and is easy to lose to a resize
   that lands between the call and the tick. Setting the logical range says the
   same thing without depending on when it is read. */
function applyFit(series) {
  const count = state.chartData?.candles.length ?? 0;
  if (count > 1) {
    series.chart.timeScale().setVisibleLogicalRange({ from: -1, to: count + 3 });
  } else {
    series.chart.timeScale().fitContent();
  }
}

/* ...and every later resize keeps the bar spacing rather than the range, so a
   window change re-crops the view. Re-fit on resize, but stop as soon as the
   operator has panned or zoomed -- silently yanking their view back would be
   worse than a cropped one. */
function wireAutoFit(series) {
  const mark = () => {
    state.userMovedChart = true;
  };
  series.container.addEventListener("wheel", mark, { passive: true });
  series.container.addEventListener("mousedown", mark);
  series.container.addEventListener("touchstart", mark, { passive: true });
  new ResizeObserver(() => {
    if (!state.userMovedChart) applyFit(series);
    positionZLegend();
  }).observe(series.container);
}

function markerStyle(marker) {
  const isShort = marker.direction === "short_umc_long_ccf";
  let color = isShort ? COLORS.down : COLORS.up;
  if (marker.kind === "entry_signal") color = isShort ? "#f4a6a4" : "#94ccc4";
  if (marker.kind === "exit_signal") color = "#b2b5be";
  if (marker.kind === "exit_fill") {
    color = marker.forced ? "#ff9800" : marker.profit ? COLORS.up : COLORS.down;
  }
  return {
    time: marker.time,
    position: marker.position,
    shape: marker.shape,
    color,
    text: marker.text,
    size: marker.kind.endsWith("signal") ? 0.8 : 1.2,
  };
}

function setZThresholds(series, rows) {
  const { entry_z: entryZ, exit_z: exitZ } = state.meta;
  const flat = (value) => rows.map((row) => ({ time: row.time, value }));
  series.zThresholds.entryUpper.setData(flat(entryZ));
  series.zThresholds.entryLower.setData(flat(-entryZ));
  // exit_z = 0 collapses both exit bands onto one line at zero; drawing it
  // twice just doubles the axis label.
  series.zThresholds.exitUpper.setData(flat(exitZ));
  series.zThresholds.exitLower.setData(Math.abs(exitZ) < 1e-9 ? [] : flat(-exitZ));
}

async function refreshLive(series) {
  const live = await api("/api/live");
  state.live = live;
  renderSidebar(live);
  renderPriceLines(series, live);
  renderStatus(live);
}

function renderPriceLines(series, live) {
  state.priceLines.forEach((line) => {
    try {
      series.candles.removePriceLine(line);
    } catch (error) {
      /* already gone */
    }
  });
  state.priceLines = [];

  const { LineStyle } = window.LightweightCharts;
  const spread = live.tick?.mid_spread ?? live.bar?.spread;
  const open = live.position_direction !== null && live.position_direction !== undefined;

  if (open && live.entry_spread !== null && live.entry_spread !== undefined) {
    const isShort = live.position_direction === "short_umc_long_ccf";
    state.priceLines.push(
      series.candles.createPriceLine({
        price: live.entry_spread,
        color: isShort ? COLORS.down : COLORS.up,
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: `entry ${isShort ? "SHORT" : "LONG"} ${Math.abs(live.ccf_contracts ?? 0)} lot`,
      })
    );
  }
  // Only while a position is open: the line exists to carry the unrealized
  // P&L, and flat it would just duplicate the candle series' own last-value
  // label at the same price.
  if (open && spread !== null && spread !== undefined) {
    const pnl = live.unrealized_pnl;
    state.priceLines.push(
      series.candles.createPriceLine({
        price: spread,
        color: pnl >= 0 ? COLORS.up : COLORS.down,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `uPnL ${money(pnl)} TWD`,
      })
    );
  }
}

function renderSidebar(live) {
  const bar = live.bar || {};
  const tick = live.tick || {};

  $("pState").textContent = STATE_LABEL[live.state] || live.state || "—";
  const dir = live.position_direction;
  const dirEl = $("pDirection");
  dirEl.textContent = dir
    ? dir === "short_umc_long_ccf"
      ? "SHORT spread · sell UMC / buy CCF"
      : "LONG spread · buy UMC / sell CCF"
    : "—";
  dirEl.className = dir ? (dir === "short_umc_long_ccf" ? "dir-short" : "dir-long") : "";

  $("pSize").textContent = dir
    ? `${Math.abs(live.ccf_contracts ?? 0)} lot / ${Math.round(Math.abs(live.umc_units ?? 0))} sh`
    : "—";
  $("pEntryZ").textContent = num(live.entry_zscore);
  $("pEntrySpread").textContent = num(live.entry_spread, 3);

  const upnl = $("pUpnl");
  upnl.textContent = dir ? money(live.unrealized_pnl) : "—";
  upnl.className = dir ? (live.unrealized_pnl >= 0 ? "pos" : "neg") : "";
  $("pUpnlNote").textContent =
    live.unrealized_source === "tick"
      ? "Model mark-to-market at the newest tick — not the broker's reported figure."
      : "Model mark-to-market at the last bar — not the broker's reported figure.";

  const longSpread = tick.long_spread ?? bar.long_spread;
  const midSpread = tick.mid_spread ?? bar.spread;
  const shortSpread = tick.short_spread ?? bar.short_spread;
  const longZ = tick.long_z ?? bar.long_z;
  const midZ = tick.mid_z ?? bar.mid_z;
  const shortZ = tick.short_z ?? bar.short_z;
  $("pLong").textContent = `${num(longSpread, 3)}   z ${num(longZ)}`;
  $("pMid").textContent = `${num(midSpread, 3)}   z ${num(midZ)}`;
  $("pShort").textContent = `${num(shortSpread, 3)}   z ${num(shortZ)}`;
  $("pBandWidth").textContent = num(live.band_width_z);

  $("pBarTime").textContent = bar.epoch ? `${clock(bar.epoch)} · ${age(bar.age_seconds)}` : "—";
  $("pTickTime").textContent = tick.observed_at
    ? `${tick.observed_at.slice(11, 19)} · ${age(tick.age_seconds)}`
    : "—";
  $("pLagNote").textContent =
    "The engine commits once per finalized minute, so a read-only viewer can trail by up to one minute.";
  $("sbTime").textContent = `updated ${new Date().toLocaleTimeString("en-GB", { hour12: false })}`;
}

function renderStatus(live) {
  const pill = $("statusPill");
  const text = $("statusText");
  const lag = live.bar?.age_seconds ?? null;
  pill.className = "pill";
  if (lag === null) {
    pill.classList.add("warn");
    text.textContent = "no bars yet";
  } else if (lag < 180) {
    pill.classList.add("ok");
    text.textContent = `live · ${age(lag)}`;
  } else if (lag < 3600) {
    pill.classList.add("warn");
    text.textContent = `lagging · ${age(lag)}`;
  } else {
    pill.classList.add("err");
    text.textContent = `stalled · ${age(lag)}`;
  }
}

function applyMeta(meta) {
  const ccf = meta.ccf_symbol === "auto" ? "CCF" : meta.ccf_symbol;
  $("symbol").textContent = `${ccf} / ${meta.umc_symbol}`;
  $("symbolSub").textContent = `spread · ${meta.fx_symbol}`;
  $("legendTitle").textContent = `${ccf}/${meta.umc_symbol} spread`;
  $("pEntryZCfg").textContent = num(meta.entry_z);
  $("pExitZCfg").textContent = num(meta.exit_z);
  $("pWindow").textContent = `${meta.zscore_window} bars`;
  $("sbStore").textContent = meta.store_path;

  const box = $("intervals");
  box.innerHTML = "";
  meta.intervals.forEach((name) => {
    const button = document.createElement("button");
    button.className = "chip" + (name === state.interval ? " active" : "");
    button.textContent = name;
    button.dataset.interval = name;
    box.appendChild(button);
  });
}

// --------------------------------------------------------------- interface
function wireToolbar(series) {
  $("intervals").addEventListener("click", async (event) => {
    const name = event.target?.dataset?.interval;
    if (!name || name === state.interval) return;
    state.interval = name;
    document
      .querySelectorAll("#intervals .chip")
      .forEach((chip) => chip.classList.toggle("active", chip.dataset.interval === name));
    await refreshChart(series, true).catch(reportError);
  });

  $("fitBtn").addEventListener("click", () => fitLater(series));

  const toggle = (id, key) =>
    $(id).addEventListener("click", (event) => {
      state[key] = !state[key];
      event.currentTarget.classList.toggle("off", !state[key]);
      refreshChart(series, false).catch(reportError);
    });
  toggle("bandBtn", "showBand");
  toggle("threshBtn", "showThresholds");
  toggle("markerBtn", "showMarkers");

  window.__chart = series.chart;
}

function wireLegend(series) {
  series.chart.subscribeCrosshairMove((param) => updateLegend(series, param));
}

function updateLegend(series, param) {
  const data = state.chartData;
  if (!data || !data.candles.length) return;

  let index = data.candles.length - 1;
  if (param?.time) {
    const found = data.candles.findIndex((candle) => candle.time === param.time);
    if (found >= 0) index = found;
  }
  const candle = data.candles[index];
  const band = data.band[index] || {};
  const z = data.z[index] || {};
  const thresholds = data.thresholds[index] || {};

  const color = candle.close >= candle.open ? COLORS.up : COLORS.down;
  const ohlc = [
    ["O", candle.open],
    ["H", candle.high],
    ["L", candle.low],
    ["C", candle.close],
  ]
    .map(
      ([label, value]) =>
        `<span><span class="k">${label}</span> <b style="color:${color}">${num(value, 3)}</b></span>`
    )
    .join("");
  $("legendOhlc").innerHTML = `<span class="k">${clock(candle.time)}</span>${ohlc}`;

  const width = z.long_z != null && z.short_z != null ? z.long_z - z.short_z : null;
  $("legendBand").innerHTML =
    `<span><i class="dot" style="background:${COLORS.long}"></i><span class="k">long</span> <b>${num(band.long, 3)}</b></span>` +
    `<span><i class="dot" style="background:${COLORS.short}"></i><span class="k">short</span> <b>${num(band.short, 3)}</b></span>` +
    `<span><span class="k">width</span> <b>${num(width)}σ</b></span>`;

  $("legendThresh").innerHTML =
    `<span><i class="dot" style="background:${COLORS.mean}"></i><span class="k">mean</span> <b>${num(thresholds.mean, 3)}</b></span>` +
    `<span><i class="dot" style="background:${COLORS.entryUpper}"></i><span class="k">+entry</span> <b>${num(thresholds.entry_upper, 3)}</b></span>` +
    `<span><i class="dot" style="background:${COLORS.entryLower}"></i><span class="k">−entry</span> <b>${num(thresholds.entry_lower, 3)}</b></span>`;

  $("legendZ").innerHTML =
    `<span><i class="dot" style="background:${COLORS.long}"></i><span class="k">long_z</span> <b>${num(z.long_z)}</b></span>` +
    `<span><i class="dot" style="background:${COLORS.short}"></i><span class="k">short_z</span> <b>${num(z.short_z)}</b></span>` +
    `<span><i class="dot" style="background:${COLORS.mean}"></i><span class="k">mid_z</span> <b>${num(z.mid_z)}</b></span>`;
}

// ------------------------------------------------------------------ errors
function reportError(error) {
  const pill = $("statusPill");
  pill.className = "pill err";
  $("statusText").textContent = String(error.message || error).slice(0, 60);
}

function fatal(message, command) {
  document.body.innerHTML = `<div class="fatal"><b>Startup failed</b><br>${message}<code>${command}</code></div>`;
}

/* The z legend belongs at the top of the lower pane, and the operator can drag
   the divider between the panes at any time -- so ask the chart where that pane
   actually starts rather than deriving it from the stretch factors it was
   created with. The estimate stays as a fallback for a chart that has not laid
   itself out yet. */
function positionZLegend() {
  const wrap = document.querySelector(".chart-wrap");
  if (!wrap) return;
  let top = Math.round((wrap.clientHeight - 28) * 0.75);
  try {
    const panes = state.chart?.panes();
    if (panes?.length > 1 && typeof panes[0].getHeight === "function") {
      top = panes[0].getHeight();
    }
  } catch (error) {
    /* fall back to the estimate */
  }
  $("legendZ").style.top = `${top + 6}px`;
}

/* The pane split is deliberately NOT managed here. setStretchFactor is a no-op
   in this build, and setHeight applied before the chart has laid itself out
   collapses the z pane to zero -- an empty pane on a screen showing a live
   position is a much worse failure than a split that is not the ratio I had in
   mind. The library's own default reads fine, and the divider is draggable. */

/* Dragging the divider resizes the panes' canvases without changing the chart
   container, so the container's ResizeObserver never fires. Watch a pane canvas
   instead. */
function watchPaneDivider() {
  const canvas = document.querySelector("#chart canvas");
  if (!canvas) return;
  new ResizeObserver(positionZLegend).observe(canvas);
}

window.addEventListener("resize", positionZLegend);

main()
  .then(positionZLegend)
  .catch((error) => fatal("Initialization failed:", String(error.stack || error)));
