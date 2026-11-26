(() => {
  const API_BASE = '';
  let token = '';
  let user = {};
  try { token = sessionStorage.getItem('c360_token') || ''; } catch {}
  try { user = JSON.parse(sessionStorage.getItem('c360_user') || '{}'); } catch {}

  if (!token) {
    window.location.href = '/static/login.html';
    return;
  }

  const $ = (id) => document.getElementById(id);
  function setText(id, value) {
    const el = $(id);
    if (el) el.textContent = value;
  }
  const wsDot = $('wsDot');
  const wsStatus = $('wsStatus');
  const wsLog = $('wsLog');
  const logoutBtn = $('logoutBtn');
  setText('userInfo', user.nombre_completo ? `${user.nombre_completo} (${user.username || ''})` : 'Monitoreo en tiempo real e históricos');

  const charts = {};
  let ws = null;
  let wsReconnectTimer = null;
  let wsPingTimer = null;
  let manualLogout = false;

  const THEME_KEY = 'c360_theme';
  const themeToggleBtn = $('themeToggleBtn');

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function syncThemeToggle() {
    if (!themeToggleBtn) return;
    const isLight = currentTheme() === 'light';
    const label = themeToggleBtn.querySelector('.theme-label');
    if (label) label.textContent = isLight ? 'Modo oscuro' : 'Modo claro';
    themeToggleBtn.setAttribute('aria-label', isLight ? 'Activar modo oscuro' : 'Activar modo claro');
    themeToggleBtn.setAttribute('aria-pressed', String(isLight));
  }

  function refreshChartThemes() {
    chartGridColor = cssVar('--chart-grid');
    chartTickColor = cssVar('--chart-tick');
    chartTooltip.backgroundColor = cssVar('--chart-tooltip-bg');
    chartTooltip.borderColor = cssVar('--chart-tooltip-border');
    chartTooltip.titleColor = cssVar('--chart-tooltip-text');
    chartTooltip.bodyColor = cssVar('--chart-tooltip-text');
    chartLegendLabels.color = chartTickColor;

    Object.values(charts).forEach((chart) => {
      const tooltip = chart.options?.plugins?.tooltip;
      if (tooltip) {
        tooltip.backgroundColor = chartTooltip.backgroundColor;
        tooltip.borderColor = chartTooltip.borderColor;
        tooltip.titleColor = chartTooltip.titleColor;
        tooltip.bodyColor = chartTooltip.bodyColor;
      }
      Object.values(chart.options?.scales || {}).forEach((scale) => {
        if (scale.grid) scale.grid.color = chartGridColor;
        if (scale.border) scale.border.color = chartGridColor;
        if (scale.ticks) scale.ticks.color = chartTickColor;
      });
      const legendLabels = chart.options?.plugins?.legend?.labels;
      if (legendLabels) legendLabels.color = chartTickColor;
      if (chart.config?.type === 'doughnut') {
        const surface = cssVar('--bg-card');
        chart.data.datasets.forEach((dataset) => { dataset.borderColor = surface; });
      }
      chart.update('none');
    });
  }

  function setTheme(theme) {
    const next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch {}
    syncThemeToggle();
    refreshChartThemes();
  }

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark'));
  }

  // Navegación por pantallas: cada pestaña muestra una sección y oculta las demás.
  // El hash de la URL (#tiempo-real, #pulso, ...) decide la pantalla, así Atrás/Adelante funcionan.
  const tabLinks = Array.from(document.querySelectorAll('.tab-link'));
  const screens = tabLinks
    .map((link) => ({ link, hash: link.getAttribute('href'), el: $(link.dataset.target) }))
    .filter((s) => s.el);

  function screenForHash(hash) {
    // También acepta los ids de sección antiguos (#seccion-historicos, ...).
    return screens.find((s) => s.hash === hash || `#${s.el.id}` === hash) || screens[0];
  }

  function showScreen(hash) {
    const active = screenForHash(hash);
    if (!active) return;
    screens.forEach((s) => {
      const isActive = s === active;
      s.el.hidden = !isActive;
      if (isActive) s.link.setAttribute('aria-current', 'page');
      else s.link.removeAttribute('aria-current');
    });
    if (hash && hash !== active.hash && `#${active.el.id}` === hash) {
      try { history.replaceState(null, '', active.hash); } catch {}
    }
    // Chart.js no mide bien un canvas oculto: se recalcula el tamaño al hacerse visible.
    requestAnimationFrame(() => {
      Object.values(charts).forEach((chart) => {
        if (active.el.contains(chart.canvas)) chart.resize();
      });
    });
  }

  window.addEventListener('hashchange', () => showScreen(window.location.hash));
  showScreen(window.location.hash);

  function authHeaders() {
    return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
  }

  function limpiarSesionYSalir() {
    manualLogout = true;
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
    if (ws) { try { ws.close(1000, 'logout'); } catch {} }
    try {
      sessionStorage.removeItem('c360_token');
      sessionStorage.removeItem('c360_user');
    } catch {}
    window.location.replace('/static/login.html');
  }

  async function apiFetch(path, options = {}) {
    const resp = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { ...(options.headers || {}), ...authHeaders() }
    });
    if (resp.status === 401) {
      limpiarSesionYSalir();
      throw new Error('No autorizado');
    }
    return resp;
  }

  async function verifySession() {
    try {
      const resp = await apiFetch('/api/auth/verify');
      if (!resp.ok) console.warn(`Verificación de sesión: HTTP ${resp.status}`);
    } catch (err) {
      console.error('No se pudo verificar la sesión', err);
    }
  }

  if (logoutBtn) {
    logoutBtn.addEventListener('click', () => {
      // Se avisa al servidor para revocar el token, pero sin esperar su respuesta:
      // la sesión local se cierra de inmediato aunque D1 o Firestore tarden o fallen.
      try {
        fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', headers: authHeaders(), keepalive: true }).catch(() => {});
      } catch {}
      limpiarSesionYSalir();
    });
  }

  function setWsStatus(connected, text) {
    if (wsDot) wsDot.className = `dot ${connected ? 'ok' : 'bad'}`;
    if (wsStatus) wsStatus.textContent = text;
  }

  // Los datos llegan con claves técnicas (arte_comisiones, rsvp_evento...); solo se traducen al mostrarlos.
  const ETIQUETAS = {
    arte_comisiones: 'Arte y comisiones', reglas_moderacion: 'Reglas y moderación',
    eventos_convenciones: 'Eventos y convenciones', videojuegos: 'Videojuegos',
    memes_humor: 'Memes y humor', fursuits: 'Fursuits',
    seguridad_privacidad: 'Seguridad y privacidad', musica: 'Música',
    mensaje: 'Mensaje', comentario: 'Comentario', reaccion: 'Reacción',
    respuesta_encuesta: 'Respuesta a encuesta', rsvp_evento: 'Confirmación a evento',
    activo: 'Activo', en_riesgo: 'En riesgo', inactivo: 'Inactivo',
    Escritorio: 'Escritorio', Web: 'Web', Android: 'Android', iOS: 'iOS'
  };
  function etiqueta(v) {
    if (v == null || v === '') return '—';
    if (ETIQUETAS[v]) return ETIQUETAS[v];
    const t = String(v).replace(/_/g, ' ');
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  function aFecha(v) {
    if (!v) return null;
    // Python manda microsegundos (.176230Z); se recortan a milisegundos para que Date los entienda siempre.
    const d = new Date(String(v).replace(/(\.\d{3})\d+/, '$1'));
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function fmtFechaHora(v) {
    const d = aFecha(v);
    return d ? d.toLocaleString('es-MX', { dateStyle: 'medium', timeStyle: 'short' }) : '—';
  }
  function fmtHora(v) {
    const d = aFecha(v) || new Date();
    return d.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }

  // Cómo se presenta cada evento en la bitácora. data-method solo da el color de la insignia.
  const EVENTOS_BITACORA = {
    conexion: { texto: 'Conexión abierta' },
    conexion_inicial: { texto: 'Conectado' },
    interaccion_creada: { texto: 'Nueva interacción', metodo: 'POST' },
    interaccion_actualizada: { texto: 'Interacción editada', metodo: 'PUT' },
    interaccion_eliminada: { texto: 'Interacción eliminada', metodo: 'DELETE' },
    error_metricas: { texto: 'Aviso', metodo: 'DELETE' }
  };
  const MENSAJE_ERROR_EN_VIVO = 'No se pudieron actualizar los datos en vivo. Reintentando…';

  function logWsEvent(evt) {
    if (!wsLog) return;
    const tipo = evt.evento || evt.type || 'evento';
    if (tipo === 'pong' || tipo === 'ack') return;
    if (wsLog.querySelector('.empty')) wsLog.innerHTML = '';
    const item = document.createElement('div');
    item.className = 'log-item';
    const info = EVENTOS_BITACORA[tipo] || { texto: etiqueta(tipo) };
    item.dataset.event = tipo;
    const actor = evt.actor ? ` · ${escapeHtml(evt.actor)}` : '';
    const detail = evt.interaccion?.id_interaccion ? ` · ${escapeHtml(evt.interaccion.id_interaccion)}` : '';
    const badge = info.metodo
      ? `<span class="method-badge" data-method="${info.metodo}">${escapeHtml(info.texto)}</span>`
      : `<span class="event-tag">${escapeHtml(info.texto)}</span>`;
    item.innerHTML = `<div class="meta"><span class="event-label">${badge}${actor}${detail}</span><span>${escapeHtml(fmtHora(evt.timestamp_utc))}</span></div><div>${summarizeEvent(evt)}</div>`;
    wsLog.prepend(item);
    while (wsLog.children.length > 30) wsLog.removeChild(wsLog.lastChild);
  }

  function summarizeEvent(evt) {
    if (evt.error) return MENSAJE_ERROR_EN_VIVO;
    if (evt.metricas) {
      const m = evt.metricas;
      return `Total: ${fmtNum(m.total_interacciones)} interacciones · Sentimiento: ${fmtNum(m.sentimiento_promedio)}`;
    }
    if ((evt.evento || evt.type) === 'conexion') return 'Esperando los datos más recientes…';
    return 'Actualización recibida.';
  }

  function fmtNum(v) {
    const n = Number(v || 0);
    if (Number.isInteger(n)) return n.toString();
    return n.toFixed(4).replace(/\.?0+$/, '');
  }

  function updateKpis(metricas) {
    setText('kpiTotal', fmtNum(metricas.total_interacciones));
    setText('kpiSentimiento', fmtNum(metricas.sentimiento_promedio));
    setText('kpiReacciones', fmtNum(metricas.reacciones_promedio));
    setText('kpiLongitud', fmtNum(metricas.longitud_media_texto));
    setText('ultimaActualizacion', `Última actualización: ${fmtFechaHora(metricas.ultima_actualizacion)}`);
  }

  function ensureChart(id, config) {
    const ctx = $(id);
    if (!ctx) return null;
    if (typeof Chart === 'undefined') {
      console.error('Chart.js no está disponible; no se pueden dibujar las gráficas.');
      return null;
    }
    if (charts[id]) {
      charts[id].data = config.data;
      charts[id].options = config.options;
      charts[id].update();
      return charts[id];
    }
    charts[id] = new Chart(ctx, config);
    return charts[id];
  }

  const palette = ['#3B6E8C', '#C86D44', '#D99B26', '#4E9A7E', '#5B8FB9', '#B85D3B', '#8C7A5B', '#6B9E8A'];
  let chartGridColor = cssVar('--chart-grid');
  let chartTickColor = cssVar('--chart-tick');
  const chartTooltip = {
    backgroundColor: cssVar('--chart-tooltip-bg'),
    borderColor: cssVar('--chart-tooltip-border'),
    borderWidth: 1,
    titleColor: cssVar('--chart-tooltip-text'),
    bodyColor: cssVar('--chart-tooltip-text'),
    cornerRadius: 8,
    padding: 10,
    boxPadding: 4
  };
  const chartLegendLabels = { color: chartTickColor, padding: 12, usePointStyle: true, boxWidth: 8 };

  const HEAT_STOPS = [[59, 110, 140], [217, 155, 38], [200, 109, 68]];
  function heatColor(ratio) {
    const t = Math.max(0, Math.min(1, ratio)) * (HEAT_STOPS.length - 1);
    const i = Math.min(Math.floor(t), HEAT_STOPS.length - 2);
    const f = t - i;
    const rgb = HEAT_STOPS[i].map((channel, k) => Math.round(channel + (HEAT_STOPS[i + 1][k] - channel) * f));
    return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${0.16 + ratio * 0.78})`;
  }

  function updateRealtimeCharts(metricas) {
    const canalLabels = Object.keys(metricas.por_canal || {});
    const canalValues = canalLabels.map(k => metricas.por_canal[k]);
    const canalNombres = canalLabels.map(etiqueta);
    ensureChart('chartCanal', {
      type: 'bar',
      data: { labels: canalNombres, datasets: [{ label: 'Interacciones', data: canalValues, backgroundColor: palette[0], borderColor: palette[4], borderWidth: 1, borderRadius: 6 }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { ...chartTooltip } },
        scales: {
          y: { beginAtZero: true, ticks: { precision: 0, color: chartTickColor }, grid: { color: chartGridColor, drawTicks: false }, border: { color: chartGridColor } },
          x: { ticks: { color: chartTickColor }, grid: { display: false }, border: { color: chartGridColor } }
        }
      }
    });

    const temaLabels = Object.keys(metricas.por_tema || {});
    const temaValues = temaLabels.map(k => metricas.por_tema[k]);
    ensureChart('chartTema', {
      type: 'doughnut',
      data: {
        labels: temaLabels.map(etiqueta),
        datasets: [{
          data: temaValues,
          backgroundColor: temaLabels.map((_, i) => palette[i % palette.length]),
          borderColor: cssVar('--bg-card'),
          borderWidth: 1,
          borderRadius: 6,
          hoverBorderRadius: 6,
          hoverBorderColor: palette[2],
          hoverOffset: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '68%',
        plugins: { legend: { position: 'bottom', labels: { ...chartLegendLabels, color: chartTickColor } }, tooltip: { ...chartTooltip } }
      }
    });
  }

  function renderHeatmap(heatmap) {
    const container = $('heatmapContainer');
    if (!container) return;
    const dias = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'];
    const bloques = ['00-04', '04-08', '08-12', '12-16', '16-20', '20-24'];
    let max = 0;
    dias.forEach(d => bloques.forEach(b => { max = Math.max(max, Number(heatmap?.[d]?.[b] || 0)); }));
    let html = '<table><thead><tr><th>Día</th>' + bloques.map(b => `<th>${b}</th>`).join('') + '</tr></thead><tbody>';
    dias.forEach(d => {
      html += `<tr><td class="day">${d}</td>`;
      bloques.forEach(b => {
        const v = Number(heatmap?.[d]?.[b] || 0);
        const ratio = max ? v / max : 0;
        const bg = heatColor(ratio);
        const color = ratio > 0.55 ? '#fff' : 'var(--text-primary)';
        html += `<td class="cell" style="background:${bg};color:${color}">${v}</td>`;
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  }

  function renderUltimas(interacciones) {
    const tbody = $('tablaInteracciones');
    if (!tbody) return;
    if (!interacciones || !interacciones.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">Sin interacciones disponibles.</td></tr>';
      return;
    }
    tbody.innerHTML = interacciones.map(i => {
      const texto = i.tipo === 'reaccion' ? '<span class="muted">— reacción —</span>' : escapeHtml((i.texto || '').slice(0, 80));
      return `<tr>
        <td>${escapeHtml(i.id_interaccion || '')}</td>
        <td>${escapeHtml(i.id_usuario || '')}</td>
        <td>${escapeHtml(fmtFechaHora(i.timestamp))}</td>
        <td><span class="pill">${escapeHtml(etiqueta(i.canal))}</span></td>
        <td>${escapeHtml(etiqueta(i.tipo))}</td>
        <td>${escapeHtml(etiqueta(i.tema))}</td>
        <td>${fmtNum(i.sentimiento)}</td>
        <td>${fmtNum(i.reacciones_recibidas)}</td>
        <td>${texto}</td>
      </tr>`;
    }).join('');
    decorateSentiment();
  }

  function decorateSentiment() {
    document.querySelectorAll('#tablaInteracciones td:nth-child(7)').forEach((cell) => {
      const value = Number(String(cell.textContent).trim());
      const tone = value > 0.05 ? 'positive' : value < -0.05 ? 'negative' : 'neutral';
      cell.dataset.sentiment = tone;
      cell.classList.add('sentiment');
    });
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function applyMetricas(metricas) {
    if (!metricas) return;
    updateKpis(metricas);
    try {
      updateRealtimeCharts(metricas);
      renderHeatmap(metricas.heatmap_actividad || {});
      renderUltimas(metricas.ultimas_interacciones || []);
    } catch (err) {
      console.error('Error al actualizar visualizaciones en tiempo real', err);
    }
  }

  function connectWs() {
    if (manualLogout) return;
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${proto}://${window.location.host}/ws?token=${encodeURIComponent(token)}`;
    setWsStatus(false, 'Conectando…');
    try { ws = new WebSocket(wsUrl); } catch { scheduleReconnect(); return; }

    ws.onopen = () => {
      wsIntentos = 0;
      setWsStatus(true, 'En vivo');
      logWsEvent({ type: 'conexion', evento: 'conexion', timestamp_utc: new Date().toISOString(), metricas: null });
      if (wsPingTimer) clearInterval(wsPingTimer);
      wsPingTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
      }, 25000);
    };

    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        console.error('Mensaje WebSocket no válido', err);
        return;
      }
      if (msg.type === 'pong') return;
      if (msg.error) {
        console.warn(msg.error);
        logWsEvent({ type: 'error_metricas', evento: 'error_metricas', timestamp_utc: new Date().toISOString(), error: msg.error });
      }
      if (msg.metricas) applyMetricas(msg.metricas);
      if (['conexion_inicial', 'interaccion_creada', 'interaccion_actualizada', 'interaccion_eliminada', 'conexion'].includes(msg.type || msg.evento)) {
        logWsEvent(msg);
      }
    };

    ws.onclose = (ev) => {
      setWsStatus(false, 'Sin conexión');
      console.warn(`WebSocket cerrado (código ${ev.code})${ev.reason ? `: ${ev.reason}` : ''}`);
      if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
      if (manualLogout) return;
      if (ev.code === 1008) { limpiarSesionYSalir(); return; }  // token inválido o revocado
      scheduleReconnect();
    };

    ws.onerror = () => {
      setWsStatus(false, 'Sin conexión');
      console.error('Error de conexión WebSocket', wsUrl);
    };
  }

  let wsIntentos = 0;
  function scheduleReconnect() {
    if (wsReconnectTimer || manualLogout) return;
    const espera = Math.min(30000, 3000 * 2 ** Math.min(wsIntentos, 4));  // 3 s, 6 s, 12 s, 24 s, 30 s
    wsIntentos += 1;
    setWsStatus(false, `Reconectando en ${Math.round(espera / 1000)} s`);
    wsReconnectTimer = setTimeout(() => { wsReconnectTimer = null; connectWs(); }, espera);
  }

  async function loadHistoricos() {
    try {
      const resp = await apiFetch('/api/historico/dashboard');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (data.error_firestore) console.error(data.error_firestore);
      renderHistoricos(data);
      const metricasHistoricas = data.metricas_tiempo_real || data.metricas_firestore;
      if (metricasHistoricas) applyMetricas(metricasHistoricas);
    } catch (err) {
      console.error('Error al cargar los históricos', err);
    }
  }

  function renderHistoricos(data) {
    const r = data.resumen_usuarios || {};
    setText('hTotalUsuarios', fmtNum(r.total_usuarios));
    setText('hConConsent', fmtNum(r.con_consentimiento_geo));
    setText('hSinConsent', fmtNum(r.sin_consentimiento_geo));
    setText('hCuarentena', fmtNum(data.interacciones_cuarentena));
    setText('bannerPrivacidad', 'Privacidad: las ciudades solo incluyen a los miembros que autorizaron compartir su ubicación.');

    const plat = data.por_plataforma_principal || [];
    ensureChart('chartPlataforma', {
      type: 'bar',
      data: { labels: plat.map(x => (x.plataforma_principal ? etiqueta(x.plataforma_principal) : 'Sin dato')), datasets: [{ label: 'Miembros', data: plat.map(x => x.total), backgroundColor: palette[3], borderColor: palette[7], borderWidth: 1, borderRadius: 6 }] },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { ...chartTooltip } },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0, color: chartTickColor }, grid: { color: chartGridColor, drawTicks: false }, border: { color: chartGridColor } },
          y: { ticks: { color: chartTickColor }, grid: { display: false }, border: { color: chartGridColor } }
        }
      }
    });

    const riesgo = data.riesgo_abandono || {};
    const riesgoLabels = ['Riesgo alto', 'Riesgo medio', 'Activos'];
    ensureChart('chartRiesgo', {
      type: 'doughnut',
      data: {
        labels: riesgoLabels,
        datasets: [{
          data: riesgoLabels.map(l => riesgo[l] || 0),
          backgroundColor: [palette[2], palette[1], palette[3]],
          borderColor: cssVar('--bg-card'),
          borderWidth: 1,
          borderRadius: 6,
          hoverBorderRadius: 6,
          hoverBorderColor: palette[2],
          hoverOffset: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '68%',
        plugins: { legend: { position: 'bottom', labels: { ...chartLegendLabels, color: chartTickColor } }, tooltip: { ...chartTooltip } }
      }
    });

    const ciudades = [...(data.top_ciudades_autorizadas || [])].sort((a, b) => Number(b.total || 0) - Number(a.total || 0));
    const ciudadesNombres = ciudades.map(c => `${c.ciudad || 'Sin dato'}${c.estado ? ', ' + c.estado : ''}`);
    const MAX_ETIQUETA = 28;
    ensureChart('chartCiudades', {
      type: 'bar',
      data: {
        labels: ciudadesNombres.map(n => (n.length > MAX_ETIQUETA ? `${n.slice(0, MAX_ETIQUETA - 1)}…` : n)),
        datasets: [{ label: 'Miembros', data: ciudades.map(c => c.total), backgroundColor: palette[0], borderColor: palette[4], borderWidth: 1, borderRadius: 4, maxBarThickness: 22 }]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { ...chartTooltip, callbacks: { title: (items) => ciudadesNombres[items[0]?.dataIndex] ?? '' } }
        },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0, color: chartTickColor }, grid: { color: chartGridColor, drawTicks: false }, border: { display: false, color: chartGridColor } },
          y: { ticks: { color: chartTickColor, autoSkip: false }, grid: { display: false }, border: { display: false, color: chartGridColor } }
        }
      }
    });
  }

  async function init() {
    syncThemeToggle();
    refreshChartThemes();
    connectWs();
    await Promise.allSettled([verifySession(), loadHistoricos()]);
  }

  init();
})();