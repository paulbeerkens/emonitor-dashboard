const REFRESH_MS = 15_000;
let selectedCircuitId = null;
let selectedUsagePeriod = "24h";
let lastPayload = null;

const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const $ = (id) => document.getElementById(id);

function watts(value) {
  return value == null ? "—" : number.format(value);
}

function storage(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

function money(value, digits = 2) {
  return value == null ? "—" : new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(value);
}

function setStatus(sampledAt, error = null) {
  const status = $("live-status");
  if (error) {
    status.className = "live-status error";
    $("status-text").textContent = "Collector unavailable";
    return;
  }
  const ageSeconds = sampledAt ? (Date.now() - sampledAt) / 1000 : Infinity;
  status.className = `live-status${ageSeconds > 45 ? " stale" : ""}`;
  $("status-text").textContent = ageSeconds > 45 ? "Waiting for a new reading" : "Live";
}

function renderSummary(data) {
  $("main-power").textContent = watts(data.main_power_watts);
  $("solar-power").textContent = watts(data.solar_power_watts);
  if (data.sampled_at_ms) {
    const date = new Date(data.sampled_at_ms);
    $("updated-time").textContent = date.toLocaleTimeString([], {
      hour: "numeric", minute: "2-digit", second: "2-digit"
    });
  } else {
    $("updated-time").textContent = "Waiting for data";
  }
  const system = data.system || {};
  $("cpu-temperature").textContent = system.cpu_temperature_c == null
    ? "—" : `${system.cpu_temperature_c.toFixed(1)} °C`;
  $("cpu-utilization").textContent = system.cpu_utilization_percent == null
    ? "—" : `${system.cpu_utilization_percent.toFixed(1)}%`;
  $("memory-use").textContent = system.memory_used_percent == null
    ? "—" : `${system.memory_used_percent.toFixed(1)}%`;
  $("disk-free").textContent = storage(system.disk_free_bytes);
  $("fan-speed").textContent = system.fan_speed_rpm == null
    ? "—" : `${number.format(system.fan_speed_rpm)} rpm`;
  $("pi-note").textContent = system.disk_free_percent == null
    ? "System readings are unavailable"
    : `${system.disk_free_percent.toFixed(1)}% of the system disk is free`;

  const pricing = data.pricing || {};
  const usage = data.last_24_hours || {};
  $("market-price").textContent = pricing.market_price_cents_per_kwh == null
    ? "—" : pricing.market_price_cents_per_kwh.toFixed(2);
  $("effective-rate").textContent = pricing.effective_variable_rate_cents_per_kwh == null
    ? "—" : `${pricing.effective_variable_rate_cents_per_kwh.toFixed(2)} ¢/kWh`;
  $("current-cost-rate").textContent = pricing.current_cost_dollars_per_hour == null
    ? "—" : money(pricing.current_cost_dollars_per_hour, 3);
  $("home-cost-24h").textContent = money(usage.cost_dollars);
  $("home-energy-24h").textContent = usage.energy_kwh == null
    ? "completed half-hour periods" : `${usage.energy_kwh.toFixed(2)} kWh of Main Power`;
  $("price-time").textContent = pricing.price_at_ms
    ? `Latest five-minute price · ${new Date(pricing.price_at_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "Waiting for the current price";
  setStatus(data.sampled_at_ms);
}

function renderSolarBarChart(svgId, tooltipId, rows, mode) {
  const svg = $(svgId);
  const tooltip = $(tooltipId);
  svg.replaceChildren();
  if (!rows.length) return;
  const width = Math.max(svg.clientWidth, 360);
  const height = Math.max(svg.clientHeight, 220);
  const margin = { top: 12, right: 12, bottom: 34, left: 48 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(...rows.map((row) => row.energy_kwh), .1) * 1.12;
  const ns = "http://www.w3.org/2000/svg";
  const make = (name, attributes = {}) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = margin.top + tick / 4 * plotHeight;
    svg.append(make("line", {
      x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "grid-line"
    }));
    const label = make("text", {
      x: margin.left - 8, y: y + 4, class: "axis-label", "text-anchor": "end"
    });
    label.textContent = `${(maximum * (1 - tick / 4)).toFixed(1)} kWh`;
    svg.append(label);
  }

  const slotWidth = plotWidth / rows.length;
  const barWidth = Math.max(3, slotWidth * .72);
  const bars = [];
  rows.forEach((row, index) => {
    const barHeight = row.energy_kwh / maximum * plotHeight;
    const x = margin.left + index * slotWidth + (slotWidth - barWidth) / 2;
    const y = margin.top + plotHeight - barHeight;
    const classes = ["solar-bar"];
    if (row.future) classes.push("future");
    if (row.current) classes.push("current");
    if (row.coverage_percent != null && row.coverage_percent < 99) classes.push("incomplete");
    const bar = make("rect", {
      x, y, width: barWidth, height: Math.max(row.future ? 1 : 2, barHeight),
      rx: Math.min(3, barWidth / 3), class: classes.join(" ")
    });
    svg.append(bar);
    bars.push({ x, y, row });
  });

  if (mode === "half-hour") {
    [...new Set(Array.from(
      { length: 5 }, (_, index) => Math.round(index * rows.length / 4)
    ))].forEach((boundary) => {
      const first = rows[0].period_start_ms;
      const timestamp = first + boundary * 30 * 60 * 1000;
      const label = make("text", {
        x: margin.left + boundary / rows.length * plotWidth,
        y: height - 9,
        class: "axis-label",
        "text-anchor": boundary === 0 ? "start" : boundary === rows.length ? "end" : "middle",
      });
      label.textContent = new Date(timestamp).toLocaleTimeString([], { hour: "numeric" });
      svg.append(label);
    });
  } else {
    [0, 3, 6, 9, rows.length - 1].forEach((index) => {
      const label = make("text", {
        x: margin.left + (index + .5) * slotWidth,
        y: height - 9,
        class: "axis-label",
        "text-anchor": index === 0 ? "start" : index === rows.length - 1 ? "end" : "middle",
      });
      label.textContent = rows[index].label;
      svg.append(label);
    });
  }

  svg.onpointermove = (event) => {
    const rect = svg.getBoundingClientRect();
    const chartX = (event.clientX - rect.left) / rect.width * width;
    const index = Math.max(0, Math.min(
      rows.length - 1, Math.floor((chartX - margin.left) / slotWidth)
    ));
    const target = bars[index];
    tooltip.hidden = false;
    tooltip.style.left = `${(target.x + barWidth / 2) / width * 100}%`;
    tooltip.style.top = `${target.y / height * 100}%`;
    const label = mode === "half-hour"
      ? new Date(target.row.period_start_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : target.row.label;
    const details = [`${target.row.energy_kwh.toFixed(3)} kWh`];
    if (mode === "half-hour" && target.row.average_power_kw != null) {
      details.push(`${target.row.average_power_kw.toFixed(2)} kW average`);
    }
    if (mode === "daily" && target.row.coverage_percent != null) {
      details.push(`${target.row.coverage_percent.toFixed(1)}% data coverage`);
    }
    if (target.row.current) details.push("updating");
    tooltip.textContent = `${label} · ${details.join(" · ")}`;
  };
  svg.onpointerleave = () => { tooltip.hidden = true; };
}

function renderSolar(data) {
  const solar = data.solar || {};
  const total = solar.today_energy_kwh;
  $("solar-today-total").textContent = total == null ? "—" : total.toFixed(2);
  $("solar-daily-latest").textContent = total == null ? "—" : total.toFixed(2);
  const dailyLegend = $("solar-daily-legend");
  const dailyLegendSwatch = $("solar-daily-legend-swatch");
  if (solar.daylight_status === "after_sunset") {
    dailyLegend.textContent = "Today · production complete";
    dailyLegendSwatch.className = "solar-swatch";
  } else if (solar.daylight_status === "before_sunrise") {
    dailyLegend.textContent = "Today · awaiting sunrise";
    dailyLegendSwatch.className = "solar-swatch";
  } else {
    dailyLegend.textContent = "Today · still updating";
    dailyLegendSwatch.className = "solar-swatch current";
  }
  renderSolarBarChart(
    "solar-today-chart", "solar-today-tooltip", solar.today_half_hours || [], "half-hour"
  );
  renderSolarBarChart(
    "solar-daily-chart", "solar-daily-tooltip", solar.daily || [], "daily"
  );
  $("solar-yesterday").textContent = solar.yesterday_by_now_kwh == null
    ? "—" : `${solar.yesterday_by_now_kwh.toFixed(2)} kWh`;
  const difference = solar.difference_percent;
  $("solar-difference").textContent = difference == null
    ? "—" : `${difference >= 0 ? "+" : ""}${difference.toFixed(1)}%`;
  const best = solar.best_day;
  $("solar-best-day").textContent = best
    ? `${best.energy_kwh.toFixed(2)} kWh · ${best.label} · ${best.coverage_percent.toFixed(1)}% coverage`
    : "—";
  const timeLabel = (timestamp) => new Date(timestamp).toLocaleTimeString([], {
    hour: "numeric", minute: "2-digit"
  });
  if (solar.horizon_start_ms != null && solar.horizon_end_ms != null) {
    $("solar-horizon").textContent = `Today · ${timeLabel(solar.horizon_start_ms)}–${timeLabel(solar.horizon_end_ms)}`;
  }
  const today = (solar.daily || []).find((day) => day.date === solar.today_date);
  const coverageEnd = solar.daylight_status === "daylight"
    ? "now" : timeLabel(solar.horizon_end_ms);
  const coverageWindow = solar.horizon_start_ms == null
    ? "daylight window" : `${timeLabel(solar.horizon_start_ms)}–${coverageEnd}`;
  $("solar-quality").textContent = today
    ? `Today’s solar data: ${today.coverage_percent.toFixed(1)}% coverage · ${today.missing_minutes.toFixed(1)} minutes missing (${coverageWindow})`
    : "Solar data coverage unavailable";
}

function renderCosts(data) {
  const rows = data.circuits || [];
  const body = $("cost-circuits");
  body.replaceChildren();
  const totalEnergy = rows.reduce((sum, row) => sum + row.energy_kwh, 0);
  rows.forEach((row, index) => {
    const share = totalEnergy ? row.energy_kwh / totalEnergy * 100 : 0;
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.setAttribute("aria-label", `Show ${row.channel_name} in the detail chart`);
    tr.innerHTML = `
      <td><span class="cost-rank">${String(index + 1).padStart(2, "0")}</span><strong></strong></td>
      <td>${row.energy_kwh.toFixed(2)} <small>kWh</small></td>
      <td>${money(row.cost_dollars, 3)}</td>
      <td><span class="share-value">${share.toFixed(1)}%</span><span class="cost-bar"><span style="width:${share}%"></span></span></td>`;
    tr.querySelector("strong").textContent = row.channel_name;
    tr.addEventListener("click", () => selectCircuit(row.channel_id));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault(); selectCircuit(row.channel_id);
      }
    });
    body.append(tr);
  });
  $("usage-period-label").textContent = data.period_label || "Selected period";
  populateUsageMonths(data.available_months || []);
  const coverage = data.priced_energy_percent;
  $("cost-coverage").textContent = coverage == null
    ? "" : `${coverage.toFixed(1)}% of circuit energy had a matching ComEd price.`;
}

function populateUsageMonths(months) {
  const select = $("usage-period");
  const existing = new Set(Array.from(select.options).map((option) => option.value));
  const missing = months.filter((month) => !existing.has(month.value));
  if (!missing.length) return;
  let group = Array.from(select.children).find(
    (item) => item.tagName === "OPTGROUP" && item.label === "Specific month"
  );
  if (!group) {
    group = document.createElement("optgroup");
    group.label = "Specific month";
    select.append(group);
  }
  missing.forEach((month) => {
    const option = document.createElement("option");
    option.value = month.value;
    option.textContent = month.label;
    group.append(option);
  });
  select.value = selectedUsagePeriod;
}

async function loadCircuitUsage() {
  try {
    const response = await fetch(
      `/api/circuit-usage?period=${encodeURIComponent(selectedUsagePeriod)}`,
      { cache: "no-store" }
    );
    if (!response.ok) {
      throw new Error((await response.json()).error || "Circuit usage request failed");
    }
    renderCosts(await response.json());
  } catch (error) {
    $("cost-coverage").textContent = error.message;
  }
}

function renderDataQuality(data) {
  const container = $("quality-days");
  container.replaceChildren();
  (data.data_quality?.days || []).forEach((day) => {
    const item = document.createElement("article");
    const severity = day.coverage_percent >= 99 ? "good"
      : day.coverage_percent >= 95 ? "fair" : "poor";
    item.className = `quality-day ${severity}`;
    item.innerHTML = `
      <div class="quality-label"><strong>${day.label}</strong><span>${day.date}</span></div>
      <div class="quality-value"><strong>${day.missing_minutes.toFixed(1)}</strong><span>minutes missing</span></div>
      <div class="quality-coverage"><span>${day.coverage_percent.toFixed(1)}% coverage</span><span class="quality-bar"><span style="width:${Math.min(100, day.coverage_percent)}%"></span></span></div>`;
    container.append(item);
  });
}

function renderBurnRate(data) {
  const svg = $("burn-chart");
  const empty = $("burn-empty");
  const history = data.pricing?.burn_rate_history || [];
  svg.replaceChildren();
  empty.hidden = history.length > 0;
  svg.hidden = history.length === 0;
  if (!history.length) return;

  const width = Math.max(svg.clientWidth, 400);
  const height = Math.max(svg.clientHeight, 210);
  const margin = { top: 14, right: 14, bottom: 35, left: 56 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(...history.map((item) => item.dollars_per_hour), .01) * 1.12;
  const ns = "http://www.w3.org/2000/svg";
  const make = (name, attributes = {}) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  for (let tick = 0; tick <= 4; tick += 1) {
    const y = margin.top + tick / 4 * plotHeight;
    svg.append(make("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "grid-line" }));
    const label = make("text", { x: margin.left - 9, y: y + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = money(maximum * (1 - tick / 4), 2); svg.append(label);
  }

  const slotWidth = plotWidth / history.length;
  const barWidth = Math.max(2, slotWidth - Math.min(3, slotWidth * .25));
  const bars = [];
  history.forEach((item, index) => {
    const barHeight = item.dollars_per_hour / maximum * plotHeight;
    const x = margin.left + index * slotWidth + (slotWidth - barWidth) / 2;
    const y = margin.top + plotHeight - barHeight;
    const bar = make("rect", {
      x, y, width: barWidth, height: Math.max(1, barHeight), rx: Math.min(3, barWidth / 2),
      class: `burn-bar${item.current ? " current" : ""}`
    });
    svg.append(bar); bars.push({ bar, x, y, item });
  });

  const labelCount = width < 650 ? 4 : 7;
  const labelIndexes = [...new Set(Array.from(
    { length: labelCount },
    (_, index) => Math.round(index * (history.length - 1) / (labelCount - 1))
  ))];
  labelIndexes.forEach((index) => {
    const x = margin.left + (index + .5) * slotWidth;
    const label = make("text", { x, y: height - 10, class: "axis-label", "text-anchor": index === 0 ? "start" : index === history.length - 1 ? "end" : "middle" });
    label.textContent = new Date(history[index].period_start_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    svg.append(label);
  });

  svg.onpointermove = (event) => {
    const rect = svg.getBoundingClientRect();
    const chartX = (event.clientX - rect.left) / rect.width * width;
    const index = Math.max(0, Math.min(history.length - 1, Math.floor((chartX - margin.left) / slotWidth)));
    const target = bars[index];
    const tooltip = $("burn-tooltip");
    tooltip.hidden = false;
    tooltip.style.left = `${(target.x + barWidth / 2) / width * 100}%`;
    tooltip.style.top = `${target.y / height * 100}%`;
    const label = target.item.current ? "Current slot" : new Date(target.item.period_start_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    tooltip.textContent = `${label} · ${money(target.item.dollars_per_hour, 3)}/hr · ${watts(target.item.average_power_watts)} W avg`;
  };
  svg.onpointerleave = () => { $("burn-tooltip").hidden = true; };
}

function donutPath(centerX, centerY, outerRadius, innerRadius, startAngle, endAngle) {
  const point = (radius, angle) => {
    const radians = (angle - 90) * Math.PI / 180;
    return { x: centerX + radius * Math.cos(radians), y: centerY + radius * Math.sin(radians) };
  };
  const outerStart = point(outerRadius, startAngle);
  const outerEnd = point(outerRadius, endAngle);
  const innerEnd = point(innerRadius, endAngle);
  const innerStart = point(innerRadius, startAngle);
  const large = endAngle - startAngle > 180 ? 1 : 0;
  return [
    `M ${outerStart.x} ${outerStart.y}`,
    `A ${outerRadius} ${outerRadius} 0 ${large} 1 ${outerEnd.x} ${outerEnd.y}`,
    `L ${innerEnd.x} ${innerEnd.y}`,
    `A ${innerRadius} ${innerRadius} 0 ${large} 0 ${innerStart.x} ${innerStart.y}`,
    "Z",
  ].join(" ");
}

function renderDonut(data) {
  const svg = $("circuit-donut");
  const legend = $("donut-legend");
  const rows = (data.last_24_hours?.circuit_breakdown || []).filter((row) => row.energy_kwh > 0);
  svg.replaceChildren(); legend.replaceChildren();
  if (!rows.length) return;
  const total = rows.reduce((sum, row) => sum + row.energy_kwh, 0);
  const ns = "http://www.w3.org/2000/svg";
  const palette = ["#68bd32", "#23a8d8", "#f5a623", "#ec6b2d", "#8b67c8", "#39b9a5", "#d94f70", "#9fbe2f", "#4c82d1", "#dc8c2f"];
  svg.setAttribute("viewBox", "0 0 240 240");
  let angle = 0;
  rows.forEach((row, index) => {
    const sweep = row.energy_kwh / total * 360;
    const color = index < palette.length
      ? palette[index]
      : `hsl(${Math.round(index * 137.508) % 360} 62% 48%)`;
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", donutPath(120, 120, 104, 59, angle, Math.min(359.999, angle + sweep)));
    path.setAttribute("fill", color);
    path.setAttribute("class", "donut-slice");
    path.setAttribute("tabindex", "0");
    path.setAttribute("aria-label", `${row.channel_name}, ${row.energy_kwh.toFixed(2)} kilowatt-hours`);
    const showTooltip = () => {
      const tooltip = $("donut-tooltip");
      tooltip.hidden = false;
      tooltip.style.left = "50%"; tooltip.style.top = "50%";
      tooltip.textContent = `${row.channel_name} · ${row.energy_kwh.toFixed(2)} kWh · ${(row.energy_kwh / total * 100).toFixed(1)}%`;
    };
    path.addEventListener("pointerenter", showTooltip);
    path.addEventListener("focus", showTooltip);
    path.addEventListener("pointerleave", () => { $("donut-tooltip").hidden = true; });
    path.addEventListener("blur", () => { $("donut-tooltip").hidden = true; });
    path.addEventListener("click", () => selectCircuit(row.channel_id));
    svg.append(path);
    angle += sweep;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "donut-legend-item";
    button.innerHTML = `<span class="donut-swatch" style="background:${color}"></span><span class="donut-name"></span><strong>${row.energy_kwh.toFixed(2)} kWh</strong><small>${(row.energy_kwh / total * 100).toFixed(1)}%</small>`;
    button.querySelector(".donut-name").textContent = row.channel_name;
    button.addEventListener("click", () => selectCircuit(row.channel_id));
    legend.append(button);
  });
  const totalText = document.createElementNS(ns, "text");
  totalText.setAttribute("x", "120"); totalText.setAttribute("y", "114");
  totalText.setAttribute("text-anchor", "middle"); totalText.setAttribute("class", "donut-total");
  totalText.textContent = total.toFixed(1);
  const unitText = document.createElementNS(ns, "text");
  unitText.setAttribute("x", "120"); unitText.setAttribute("y", "136");
  unitText.setAttribute("text-anchor", "middle"); unitText.setAttribute("class", "donut-unit");
  unitText.textContent = "branch kWh";
  svg.append(totalText, unitText);
}

function renderCircuits(data) {
  const container = $("top-circuits");
  container.replaceChildren();
  const maximum = Math.max(...data.top_circuits.map((item) => item.power_watts), 1);
  data.top_circuits.forEach((circuit) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `circuit-card${circuit.channel_id === selectedCircuitId ? " selected" : ""}`;
    button.setAttribute("aria-label", `Show ${circuit.channel_name}, ${circuit.power_watts} watts`);
    button.innerHTML = `
      <span class="circuit-name"></span>
      <span class="circuit-watts">${watts(circuit.power_watts)} <small>W</small></span>
      <span class="power-bar"><span style="width:${Math.max(2, circuit.power_watts / maximum * 100)}%"></span></span>`;
    button.querySelector(".circuit-name").textContent = circuit.channel_name;
    button.addEventListener("click", () => selectCircuit(circuit.channel_id));
    container.append(button);
  });
}

function renderPicker(data) {
  const select = $("circuit-select");
  const previousIds = Array.from(select.options).map((option) => Number(option.value));
  const nextIds = data.circuits.map((circuit) => circuit.id);
  if (previousIds.join(",") !== nextIds.join(",")) {
    select.replaceChildren(...data.circuits.map((circuit) => {
      const option = document.createElement("option");
      option.value = circuit.id;
      option.textContent = circuit.name;
      return option;
    }));
  }
  if (data.selected_circuit) {
    selectedCircuitId = data.selected_circuit.id;
    select.value = selectedCircuitId;
    $("chart-title").textContent = data.selected_circuit.name;
  }
}

function chartPoint(history, index, width, height, margin, maximum) {
  const x = margin.left + index / Math.max(history.length - 1, 1) * (width - margin.left - margin.right);
  const y = margin.top + (1 - history[index].watts / maximum) * (height - margin.top - margin.bottom);
  return { x, y };
}

function renderChart(data) {
  const svg = $("power-chart");
  const empty = $("empty-state");
  const history = data.history;
  $("sample-count").textContent = number.format(history.length);
  $("chart-now").textContent = history.length ? `${watts(history.at(-1).watts)} W` : "—";
  $("chart-peak").textContent = history.length ? `${watts(Math.max(...history.map((p) => p.watts)))} W` : "—";
  svg.replaceChildren();
  empty.hidden = history.length > 0;
  svg.hidden = history.length === 0;
  if (!history.length) return;

  const width = Math.max(svg.clientWidth, 400);
  const height = Math.max(svg.clientHeight, 280);
  const margin = { top: 18, right: 16, bottom: 35, left: 54 };
  const maximum = Math.max(...history.map((point) => point.watts), 10) * 1.08;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const ns = "http://www.w3.org/2000/svg";
  const make = (name, attributes = {}) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };

  const defs = make("defs");
  const gradient = make("linearGradient", { id: "area-gradient", x1: "0", y1: "0", x2: "0", y2: "1" });
  gradient.append(make("stop", { offset: "0%", "stop-color": "#68bd32", "stop-opacity": ".28" }));
  gradient.append(make("stop", { offset: "100%", "stop-color": "#68bd32", "stop-opacity": "0" }));
  defs.append(gradient); svg.append(defs);

  for (let tick = 0; tick <= 4; tick += 1) {
    const y = margin.top + tick / 4 * (height - margin.top - margin.bottom);
    svg.append(make("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "grid-line" }));
    const label = make("text", { x: margin.left - 10, y: y + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = `${watts(maximum * (1 - tick / 4))} W`; svg.append(label);
  }
  for (let tick = 0; tick <= 4; tick += 1) {
    const x = margin.left + tick / 4 * (width - margin.left - margin.right);
    const timestamp = history[0].timestamp_ms + tick / 4 * (history.at(-1).timestamp_ms - history[0].timestamp_ms);
    const label = make("text", { x, y: height - 10, class: "axis-label", "text-anchor": tick === 0 ? "start" : tick === 4 ? "end" : "middle" });
    label.textContent = new Date(timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); svg.append(label);
  }

  const points = history.map((_, index) => chartPoint(history, index, width, height, margin, maximum));
  const linePath = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const baseline = height - margin.bottom;
  svg.append(make("path", { d: `${linePath} L${points.at(-1).x},${baseline} L${points[0].x},${baseline} Z`, class: "area-fill" }));
  svg.append(make("path", { d: linePath, class: "power-line" }));

  const hoverLine = make("line", { y1: margin.top, y2: baseline, class: "hover-line", visibility: "hidden" });
  const hoverDot = make("circle", { r: 5, class: "hover-dot", visibility: "hidden" });
  svg.append(hoverLine, hoverDot);
  svg.onpointermove = (event) => {
    const rect = svg.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width * width;
    const ratio = Math.max(0, Math.min(1, (x - margin.left) / (width - margin.left - margin.right)));
    const index = Math.round(ratio * (history.length - 1));
    const point = points[index];
    hoverLine.setAttribute("x1", point.x); hoverLine.setAttribute("x2", point.x); hoverLine.setAttribute("visibility", "visible");
    hoverDot.setAttribute("cx", point.x); hoverDot.setAttribute("cy", point.y); hoverDot.setAttribute("visibility", "visible");
    const tooltip = $("chart-tooltip");
    tooltip.hidden = false;
    tooltip.style.left = `${point.x / width * 100}%`;
    tooltip.style.top = `${point.y / height * 100}%`;
    tooltip.textContent = `${new Date(history[index].timestamp_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })} · ${watts(history[index].watts)} W`;
  };
  svg.onpointerleave = () => {
    hoverLine.setAttribute("visibility", "hidden"); hoverDot.setAttribute("visibility", "hidden"); $("chart-tooltip").hidden = true;
  };
}

async function loadDashboard() {
  const query = selectedCircuitId == null ? "" : `?circuit_id=${selectedCircuitId}`;
  try {
    const response = await fetch(`/api/dashboard${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).error || "Dashboard request failed");
    const data = await response.json();
    lastPayload = data;
    renderSummary(data);
    renderPicker(data);
    renderCircuits(data);
    renderChart(data);
    renderDataQuality(data);
    renderBurnRate(data);
    renderDonut(data);
    renderSolar(data);
  } catch (error) {
    setStatus(null, error);
    $("updated-time").textContent = error.message;
  }
}

function selectCircuit(circuitId) {
  selectedCircuitId = circuitId;
  loadDashboard();
}

$("circuit-select").addEventListener("change", (event) => selectCircuit(Number(event.target.value)));
$("usage-period").addEventListener("change", (event) => {
  selectedUsagePeriod = event.target.value;
  loadCircuitUsage();
});
window.addEventListener("resize", () => {
  if (lastPayload) {
    renderChart(lastPayload);
    renderBurnRate(lastPayload);
    renderDonut(lastPayload);
    renderSolar(lastPayload);
  }
});
loadDashboard();
loadCircuitUsage();
setInterval(() => {
  loadDashboard();
  if (selectedUsagePeriod === "24h") loadCircuitUsage();
}, REFRESH_MS);

