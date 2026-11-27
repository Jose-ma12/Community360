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
  setText('userInfo', user.nombre_completo || 'Panel de la comunidad');

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
    aplicarDefaultsChart();
    chartGridColor = colorRejilla();
    chartTickColor = colorTexto();
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
      // Las ciudades usan el color de acento del tema, que cambia entre claro y oscuro.
      if (chart.canvas?.id === 'chartCiudades') {
        const acento = cssVar('--acento');
        chart.data.datasets.forEach((dataset) => { dataset.backgroundColor = acento; dataset.borderColor = acento; });
      }
      chart.update();
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
    return d ? d.toLocaleString('es-MX', { dateStyle: 'medium', timeStyle: 'short', hourCycle: 'h23' }) : '—';
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

  const PALETA = ['#8FB3E0', '#7E9A8C', '#B9925C', '#C8814F', '#6F86A6', '#A3B8A9', '#9C8566', '#5E6E80'];

  function colorTexto() {
    return getComputedStyle(document.documentElement).getPropertyValue('--texto-2').trim() || '#9AA7B6';
  }

  function colorRejilla() {
    return currentTheme() === 'light' ? 'rgba(52,84,127,.10)' : 'rgba(143,179,224,.08)';
  }

  function aplicarDefaultsChart() {
    if (typeof Chart === 'undefined') return;
    Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
    Chart.defaults.color = colorTexto();
    Chart.defaults.borderColor = colorRejilla();
    Chart.defaults.elements.line.borderWidth = 2;
  }
  aplicarDefaultsChart();

  let chartGridColor = colorRejilla();
  let chartTickColor = colorTexto();
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

  // Escala del mapa de calor: de --tarjeta-2 (sin actividad) a --acento (máxima). Al usar variables CSS
  // se adapta sola al cambiar de tema.
  function heatColor(ratio) {
    const pct = Math.round(Math.max(0, Math.min(1, ratio)) * 100);
    return `color-mix(in srgb, var(--acento) ${pct}%, var(--tarjeta-2))`;
  }

  function updateRealtimeCharts(metricas) {
    const canalLabels = Object.keys(metricas.por_canal || {});
    const canalValues = canalLabels.map(k => metricas.por_canal[k]);
    const canalNombres = canalLabels.map(etiqueta);
    ensureChart('chartCanal', {
      type: 'bar',
      data: { labels: canalNombres, datasets: [{ label: 'Interacciones', data: canalValues, backgroundColor: PALETA[0], borderColor: PALETA[0], borderWidth: 0, borderRadius: 4 }] },
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
          backgroundColor: temaLabels.map((_, i) => PALETA[i % PALETA.length]),
          borderColor: cssVar('--bg-card'),
          borderWidth: 1,
          borderRadius: 4,
          hoverBorderRadius: 4,
          hoverBorderColor: cssVar('--bg-card'),
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
        const color = ratio > 0.55 ? 'var(--bg)' : 'var(--texto)';
        html += `<td class="cell" style="background:${bg};color:${color}">${v}</td>`;
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  }

  function decorateSentiment() {
    document.querySelectorAll('#tablaInteracciones td:nth-child(7)').forEach((cell) => {
      cell.dataset.sentiment = tonoSentimiento(String(cell.textContent).trim());
      cell.classList.add('sentiment');
    });
  }

  // Mismo umbral para colorear la tabla, filtrar por sentimiento y exportar.
  function tonoSentimiento(v) {
    const n = Number(v);
    return n > 0.05 ? 'positive' : n < -0.05 ? 'negative' : 'neutral';
  }
  const TONOS = { positive: 'Positivo', neutral: 'Neutral', negative: 'Negativo' };

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // nuevo = la actualización viene de un alta, edición o borrado (no de la conexión inicial).
  function applyMetricas(metricas, { nuevo = false } = {}) {
    if (!metricas) return;
    updateKpis(metricas);
    try {
      updateRealtimeCharts(metricas);
      renderHeatmap(metricas.heatmap_actividad || {});
      actualizarTablaDesdeMetricas(metricas, nuevo);
    } catch (err) {
      console.error('Error al actualizar visualizaciones en tiempo real', err);
    }
  }

  function connectWs() {
    if (manualLogout) return;
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${proto}://${window.location.host}/ws`;
    setWsStatus(false, 'Conectando…');
    try { ws = new WebSocket(wsUrl); } catch { scheduleReconnect(); return; }

    ws.onopen = () => {
      wsIntentos = 0;
      // El token va en el primer mensaje, no en la URL (así no aparece en los registros del servidor).
      ws.send(JSON.stringify({ type: 'auth', token }));
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
      if (msg.type === 'conexion_inicial') setWsStatus(true, 'En vivo');
      if (msg.error) {
        console.warn(msg.error);
        logWsEvent({ type: 'error_metricas', evento: 'error_metricas', timestamp_utc: new Date().toISOString(), error: msg.error });
      }
      if (msg.metricas) applyMetricas(msg.metricas, { nuevo: EVENTOS_CAMBIO.has(msg.type || msg.evento) });
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
    llenarSelect($('pPlataforma'), plat.filter(x => x.plataforma_principal).map(x => ({ valor: x.plataforma_principal, texto: etiqueta(x.plataforma_principal) })));
    ensureChart('chartPlataforma', {
      type: 'bar',
      data: { labels: plat.map(x => (x.plataforma_principal ? etiqueta(x.plataforma_principal) : 'Sin dato')), datasets: [{ label: 'Miembros', data: plat.map(x => x.total), backgroundColor: PALETA[1], borderColor: PALETA[1], borderWidth: 0, borderRadius: 4 }] },
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
          backgroundColor: [PALETA[3], PALETA[2], PALETA[1]],
          borderColor: cssVar('--bg-card'),
          borderWidth: 1,
          borderRadius: 4,
          hoverBorderRadius: 4,
          hoverBorderColor: cssVar('--bg-card'),
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
        datasets: [{ label: 'Miembros', data: ciudades.map(c => c.total), backgroundColor: cssVar('--acento'), borderColor: cssVar('--acento'), borderWidth: 0, borderRadius: 4, maxBarThickness: 22 }]
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

  // ── Utilidades de consulta y paginación ─────────────────────────────────
  const EVENTOS_CAMBIO = new Set(['interaccion_creada', 'interaccion_actualizada', 'interaccion_eliminada']);
  const MAX_LOTES = 40;  // tope de seguridad al recorrer una colección completa por lotes

  function debounce(fn, ms) {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function fmtEntero(v) {
    return Number(v || 0).toLocaleString('es-MX');
  }

  async function getJson(ruta) {
    const resp = await apiFetch(ruta);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  // Trae todos los registros de un endpoint paginado (limit/offset). Se guarda la promesa en caché
  // por combinación de filtros, así teclear en el buscador no repite consultas al servidor.
  function traerTodas(ruta, params, clave, tamLote, cache) {
    const llave = params.toString();
    if (!cache.has(llave)) {
      const promesa = (async () => {
        const todas = [];
        let total = Infinity;
        for (let lote = 0; lote < MAX_LOTES && todas.length < total; lote += 1) {
          const p = new URLSearchParams(params);
          p.set('limit', tamLote);
          p.set('offset', todas.length);
          const data = await getJson(`${ruta}?${p}`);
          const filas = data[clave] || [];
          total = Number(data.total) || 0;
          todas.push(...filas);
          if (!filas.length) break;
        }
        return todas;
      })();
      cache.set(llave, promesa);
      promesa.catch(() => cache.delete(llave));
    }
    return cache.get(llave);
  }

  function paginar(lista, pagina, limite) {
    const paginas = Math.max(1, Math.ceil(lista.length / limite));
    const p = Math.min(pagina, paginas - 1);
    return { pagina: p, filas: lista.slice(p * limite, (p + 1) * limite) };
  }

  // prefijo: pagInter, pagPadron o pagCuar (ids ...Info, ...Anterior y ...Siguiente del HTML).
  function renderPager(prefijo, estado, limite) {
    const desde = estado.filas.length ? estado.pagina * limite + 1 : 0;
    const hasta = desde ? desde + estado.filas.length - 1 : 0;
    setText(`${prefijo}Info`, `Mostrando ${fmtEntero(desde)}–${fmtEntero(hasta)} de ${fmtEntero(estado.total)}`);
    const anterior = $(`${prefijo}Anterior`);
    const siguiente = $(`${prefijo}Siguiente`);
    if (anterior) anterior.disabled = estado.pagina === 0;
    if (siguiente) siguiente.disabled = hasta >= estado.total;
  }

  function setBusy(id, busy) {
    const el = $(id);
    if (el) el.setAttribute('aria-busy', String(busy));
  }

  function filaVacia(tbodyId, columnas, mensaje) {
    const tbody = $(tbodyId);
    if (tbody) tbody.innerHTML = `<tr><td colspan="${columnas}" class="empty">${escapeHtml(mensaje)}</td></tr>`;
  }

  // Conserva la primera opción ("Todas"/"Todos") y la selección actual si sigue existiendo.
  function llenarSelect(select, opciones) {
    if (!select) return;
    const actual = select.value;
    select.replaceChildren(select.options[0], ...opciones.map(o => new Option(o.texto, o.valor)));
    if (opciones.some(o => o.valor === actual)) select.value = actual;
  }

  // ── Interacciones: filtros, paginación, detalle y CSV ───────────────────
  // Canal, tema y tipo los filtra la API. El texto y el sentimiento se filtran aquí sobre la lista
  // completa de /api/interacciones, que se pide una sola vez por combinación de filtros.
  const filtrosInter = {
    buscar: $('fBuscar'), canal: $('fCanal'), tema: $('fTema'), tipo: $('fTipo'),
    sentimiento: $('fSentimiento'), filas: $('fFilas')
  };
  const inter = { pagina: 0, total: 0, filas: [], peticion: 0, cargada: false, enCurso: false };
  const cacheInteracciones = new Map();
  const avisoNuevas = $('avisoNuevas');

  function leerFiltrosInter() {
    return {
      q: (filtrosInter.buscar?.value || '').trim(),
      canal: filtrosInter.canal?.value || '',
      tema: filtrosInter.tema?.value || '',
      tipo: filtrosInter.tipo?.value || '',
      sentimiento: filtrosInter.sentimiento?.value || '',
      limite: Number(filtrosInter.filas?.value) || 10
    };
  }
  function hayFiltrosInter(f) {
    return Boolean(f.q || f.canal || f.tema || f.tipo || f.sentimiento);
  }
  // Sin filtros y en la primera página la tabla se actualiza sola con cada evento del WebSocket.
  function tablaEnVivo(f) {
    return !hayFiltrosInter(f) && inter.pagina === 0;
  }

  function mostrarAvisoNuevas(visible) {
    if (avisoNuevas) avisoNuevas.hidden = !visible;
  }

  function coincideInteraccion(i, f) {
    if (f.sentimiento && tonoSentimiento(i.sentimiento) !== f.sentimiento) return false;
    if (!f.q) return true;
    const q = f.q.toLowerCase();
    return [i.id_interaccion, i.id_usuario, i.texto].some(v => String(v || '').toLowerCase().includes(q));
  }

  function pintarInteracciones(filas, filtrado) {
    inter.filas = filas || [];
    const tbody = $('tablaInteracciones');
    if (tbody) {
      tbody.innerHTML = inter.filas.length
        ? inter.filas.map((i, idx) => {
          const texto = i.tipo === 'reaccion' ? '<span class="muted">— reacción —</span>' : escapeHtml((i.texto || '').slice(0, 80));
          return `<tr class="row-link" data-idx="${idx}" tabindex="0" title="Ver detalle">
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
        }).join('')
        : `<tr><td colspan="9" class="empty">${filtrado ? 'Ninguna interacción coincide con los filtros.' : 'Sin interacciones disponibles.'}</td></tr>`;
      decorateSentiment();
    }
    renderPager('pagInter', inter, leerFiltrosInter().limite);
    const btnCsv = $('btnExportarCsv');
    if (btnCsv) btnCsv.disabled = !inter.filas.length;
  }

  async function cargarInteracciones() {
    const f = leerFiltrosInter();
    const peticion = ++inter.peticion;  // descarta respuestas viejas si los filtros cambian a media consulta
    inter.enCurso = true;
    mostrarAvisoNuevas(false);
    setBusy('wrapInteracciones', true);
    try {
      const params = new URLSearchParams();
      ['canal', 'tema', 'tipo'].forEach((k) => { if (f[k]) params.set(k, f[k]); });
      let pagina = inter.pagina;
      let filas;
      let total;
      if (f.q || f.sentimiento) {
        const todas = (await traerTodas('/api/interacciones', params, 'interacciones', 500, cacheInteracciones))
          .filter(i => coincideInteraccion(i, f));
        ({ pagina, filas } = paginar(todas, pagina, f.limite));
        total = todas.length;
      } else {
        params.set('limit', f.limite);
        params.set('offset', pagina * f.limite);
        const data = await getJson(`/api/interacciones?${params}`);
        total = Number(data.total) || 0;
        filas = data.interacciones || [];
      }
      if (peticion !== inter.peticion) return;
      Object.assign(inter, { pagina, total, cargada: true });
      pintarInteracciones(filas, hayFiltrosInter(f));
    } catch (err) {
      if (peticion !== inter.peticion) return;
      console.error('Error al cargar las interacciones', err);
      Object.assign(inter, { filas: [], total: 0 });
      filaVacia('tablaInteracciones', 9, 'No se pudieron cargar las interacciones. Intenta de nuevo.');
      renderPager('pagInter', inter, f.limite);
    } finally {
      if (peticion === inter.peticion) {
        inter.enCurso = false;
        setBusy('wrapInteracciones', false);
      }
    }
  }
  // Varios eventos seguidos del WebSocket se juntan en una sola consulta.
  const cargarInteraccionesEnVivo = debounce(cargarInteracciones, 500);

  // La tabla y el contador "Mostrando X–Y de Z" siempre salen de GET /api/interacciones,
  // así el total y los botones Anterior/Siguiente coinciden con lo que hay en Firestore.
  function actualizarTablaDesdeMetricas(metricas, nuevo) {
    if (nuevo) cacheInteracciones.clear();
    if (!tablaEnVivo(leerFiltrosInter())) {
      // Con filtros activos no se mueve la tabla mientras se lee: solo se avisa.
      if (nuevo) mostrarAvisoNuevas(true);
      return;
    }
    if (nuevo) cargarInteraccionesEnVivo();
    else if (!inter.cargada && !inter.enCurso) cargarInteracciones();
  }

  function irAPrimeraPagina() {
    inter.pagina = 0;
    cargarInteracciones();
  }

  // Al entrar a la pantalla Interacciones sin filtros se vuelve a pedir la primera página
  // (limit=10&offset=0 por defecto) para que el contador muestre el total real del momento.
  window.addEventListener('hashchange', () => {
    if (screenForHash(window.location.hash).el.id === 'tabla-recientes' && tablaEnVivo(leerFiltrosInter())) {
      cargarInteracciones();
    }
  });

  filtrosInter.buscar?.addEventListener('input', debounce(irAPrimeraPagina, 300));
  ['canal', 'tema', 'tipo', 'sentimiento', 'filas'].forEach((k) => filtrosInter[k]?.addEventListener('change', irAPrimeraPagina));
  $('btnLimpiarFiltros')?.addEventListener('click', () => {
    if (filtrosInter.buscar) filtrosInter.buscar.value = '';
    ['canal', 'tema', 'tipo', 'sentimiento'].forEach((k) => { if (filtrosInter[k]) filtrosInter[k].value = ''; });
    if (filtrosInter.filas) filtrosInter.filas.value = '10';
    irAPrimeraPagina();
    filtrosInter.buscar?.focus();
  });
  $('pagInterAnterior')?.addEventListener('click', () => { inter.pagina = Math.max(0, inter.pagina - 1); cargarInteracciones(); });
  $('pagInterSiguiente')?.addEventListener('click', () => { inter.pagina += 1; cargarInteracciones(); });
  $('btnRefrescar')?.addEventListener('click', () => { cacheInteracciones.clear(); cargarInteracciones(); });

  // Detalle en un <dialog> nativo: atrapa el foco, cierra con Esc y lo devuelve a la fila al cerrar.
  const dialogo = $('detalleDialog');
  let detalleActual = '';

  function nombreIdioma(codigo) {
    if (!codigo) return '—';
    try {
      const nombre = new Intl.DisplayNames(['es'], { type: 'language' }).of(codigo);
      return nombre && nombre !== codigo ? `${nombre.charAt(0).toUpperCase()}${nombre.slice(1)} (${codigo})` : codigo;
    } catch {
      return codigo;
    }
  }

  function datoDetalle(titulo, valorHtml, ancho = false) {
    return `<div class="detail-item${ancho ? ' detail-item--wide' : ''}"><dt>${titulo}</dt><dd>${valorHtml}</dd></div>`;
  }

  function pintarDetalle(i, cargando) {
    const pendiente = cargando ? '<span class="muted">Cargando…</span>' : '—';
    const tono = tonoSentimiento(i.sentimiento);
    let etiquetas = pendiente;
    if (Array.isArray(i.etiquetas)) {
      etiquetas = i.etiquetas.length
        ? `<span class="tag-list">${i.etiquetas.map(e => `<span class="pill">${escapeHtml(e)}</span>`).join('')}</span>`
        : '<span class="muted">Sin etiquetas</span>';
    }
    let texto = '<span class="muted">Sin texto</span>';
    if (i.tipo === 'reaccion') texto = '<span class="muted">Las reacciones no llevan texto.</span>';
    else if (i.texto) texto = `<p class="detail-text">${escapeHtml(i.texto)}</p>`;

    setText('detalleTitulo', `Interacción ${i.id_interaccion || ''}`.trim());
    const cuerpo = $('detalleCuerpo');
    if (!cuerpo) return;
    cuerpo.innerHTML = `<dl class="detail-grid">
      ${datoDetalle('Folio', `<span class="mono">${escapeHtml(i.id_interaccion || '—')}</span>`)}
      ${datoDetalle('Usuario', `<span class="mono">${escapeHtml(i.id_usuario || '—')}</span>`)}
      ${datoDetalle('Fecha y hora', escapeHtml(fmtFechaHora(i.timestamp)))}
      ${datoDetalle('Canal', `<span class="pill">${escapeHtml(etiqueta(i.canal))}</span>`)}
      ${datoDetalle('Tipo', escapeHtml(etiqueta(i.tipo)))}
      ${datoDetalle('Tema', escapeHtml(etiqueta(i.tema)))}
      ${datoDetalle('Sentimiento', `<span class="sentiment" data-sentiment="${tono}">${fmtNum(i.sentimiento)} · ${TONOS[tono]}</span>`)}
      ${datoDetalle('Reacciones', `<span class="mono">${fmtNum(i.reacciones_recibidas)}</span>`)}
      ${datoDetalle('Dispositivo', i.dispositivo ? escapeHtml(etiqueta(i.dispositivo)) : pendiente)}
      ${datoDetalle('Idioma', i.idioma ? escapeHtml(nombreIdioma(i.idioma)) : pendiente)}
      ${datoDetalle('Etiquetas', etiquetas, true)}
      ${datoDetalle('Texto completo', texto, true)}
    </dl>`;
  }

  async function abrirDetalle(i) {
    if (!dialogo || !i) return;
    detalleActual = i.id_interaccion || '';
    // Las filas que llegan por WebSocket no traen idioma, dispositivo ni etiquetas: se piden aparte.
    const completa = 'idioma' in i && 'dispositivo' in i && 'etiquetas' in i;
    pintarDetalle(i, !completa && Boolean(detalleActual));
    if (!dialogo.open) {
      if (typeof dialogo.showModal === 'function') dialogo.showModal();
      else dialogo.setAttribute('open', '');
    }
    if (completa || !detalleActual) return;
    try {
      const data = await getJson(`/api/interacciones/${encodeURIComponent(detalleActual)}`);
      if (dialogo.open && detalleActual === data.id_interaccion) pintarDetalle(data, false);
    } catch (err) {
      console.error('No se pudo cargar el detalle de la interacción', err);
      if (dialogo.open && detalleActual === i.id_interaccion) pintarDetalle(i, false);
    }
  }

  function cerrarDetalle() {
    if (!dialogo) return;
    if (typeof dialogo.close === 'function') dialogo.close();
    else dialogo.removeAttribute('open');
  }

  $('detalleCerrar')?.addEventListener('click', cerrarDetalle);
  // Clic en el fondo oscuro (fuera de .modal-inner) cierra el detalle.
  dialogo?.addEventListener('click', (ev) => { if (ev.target === dialogo) cerrarDetalle(); });

  const tbodyInter = $('tablaInteracciones');
  tbodyInter?.addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-idx]');
    if (tr) abrirDetalle(inter.filas[Number(tr.dataset.idx)]);
  });
  tbodyInter?.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const tr = ev.target.closest('tr[data-idx]');
    if (!tr) return;
    ev.preventDefault();
    abrirDetalle(inter.filas[Number(tr.dataset.idx)]);
  });

  // CSV de las filas visibles. Los textos que empiezan con = + - @ se prefijan con ' para que
  // Excel no los ejecute como fórmula; el BOM inicial hace que Excel respete los acentos.
  function celdaCsv(v) {
    if (v == null) return '';
    let s = String(v);
    if (typeof v !== 'number' && /^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",;\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function exportarCsv() {
    if (!inter.filas.length) return;
    const columnas = [
      ['Folio', i => i.id_interaccion],
      ['Usuario', i => i.id_usuario],
      ['Fecha y hora (UTC)', i => i.timestamp],
      ['Canal', i => etiqueta(i.canal)],
      ['Tipo', i => etiqueta(i.tipo)],
      ['Tema', i => etiqueta(i.tema)],
      ['Sentimiento', i => (i.sentimiento == null ? null : Number(i.sentimiento))],
      ['Tono', i => TONOS[tonoSentimiento(i.sentimiento)]],
      ['Reacciones', i => (i.reacciones_recibidas == null ? null : Number(i.reacciones_recibidas))],
      ['Texto', i => (i.tipo === 'reaccion' ? '' : i.texto)]
    ];
    const lineas = [
      columnas.map(([titulo]) => celdaCsv(titulo)).join(','),
      ...inter.filas.map(i => columnas.map(([, valor]) => celdaCsv(valor(i))).join(','))
    ];
    const blob = new Blob([`﻿${lineas.join('\r\n')}\r\n`], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const sello = new Date().toISOString().slice(0, 16).replace(/[-:]/g, '').replace('T', '-');
    const enlace = document.createElement('a');
    enlace.href = url;
    enlace.download = `interacciones_${sello}.csv`;
    document.body.appendChild(enlace);
    enlace.click();
    enlace.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $('btnExportarCsv')?.addEventListener('click', exportarCsv);

  // ── Explorador histórico (D1): padrón y cuarentena ──────────────────────
  const filtrosPadron = { buscar: $('pBuscar'), plataforma: $('pPlataforma'), artista: $('pArtista'), filas: $('pFilas') };
  const padron = { pagina: 0, total: 0, filas: [], peticion: 0, cargada: false };
  const cachePadron = new Map();

  function leerFiltrosPadron() {
    return {
      q: (filtrosPadron.buscar?.value || '').trim(),
      plataforma: filtrosPadron.plataforma?.value || '',
      artista: filtrosPadron.artista?.value || '',
      limite: Number(filtrosPadron.filas?.value) || 25
    };
  }

  // La API ya omite la ubicación de quien no dio consentimiento; aquí se dice explícitamente.
  function ubicacionMiembro(u) {
    if (Number(u.consentimiento_datos) === 0) {
      return '<span class="privacy-tag" title="El miembro no autorizó compartir su ubicación">Oculto por privacidad</span>';
    }
    const partes = [u.ciudad, u.estado, u.pais].filter(Boolean);
    return partes.length ? escapeHtml(partes.join(', ')) : '<span class="muted">Sin dato</span>';
  }

  function pintarPadron(filas, filtrado) {
    padron.filas = filas || [];
    const tbody = $('tablaPadron');
    if (tbody) {
      tbody.innerHTML = padron.filas.length
        ? padron.filas.map(u => `<tr>
            <td class="mono">${escapeHtml(u.id_usuario || '')}</td>
            <td>${escapeHtml(u.alias || '—')}</td>
            <td>${escapeHtml(etiqueta(u.plataforma_principal))}</td>
            <td>${escapeHtml(etiqueta(u.interes_principal))}</td>
            <td>${Number(u.es_artista) === 1 ? 'Sí' : 'No'}</td>
            <td>${escapeHtml(etiqueta(u.estatus))}</td>
            <td class="mono">${u.mensajes_30d == null ? '—' : fmtNum(u.mensajes_30d)}</td>
            <td class="mono">${escapeHtml(fmtFechaHora(u.ultima_actividad))}</td>
            <td>${ubicacionMiembro(u)}</td>
          </tr>`).join('')
        : `<tr><td colspan="9" class="empty">${filtrado ? 'Ningún miembro coincide con los filtros.' : 'Sin miembros registrados.'}</td></tr>`;
    }
    renderPager('pagPadron', padron, leerFiltrosPadron().limite);
  }

  async function cargarPadron() {
    const f = leerFiltrosPadron();
    const peticion = ++padron.peticion;
    setBusy('wrapPadron', true);
    try {
      const params = new URLSearchParams();
      if (f.plataforma) params.set('plataforma', f.plataforma);
      if (f.artista !== '') params.set('es_artista', f.artista);
      let pagina = padron.pagina;
      let filas;
      let total;
      if (f.q) {
        // El endpoint no busca por texto: se recorre el padrón filtrado (lotes de 200) y se busca aquí.
        const q = f.q.toLowerCase();
        const todos = (await traerTodas('/api/historico/usuarios', params, 'usuarios', 200, cachePadron))
          .filter(u => [u.id_usuario, u.alias].some(v => String(v || '').toLowerCase().includes(q)));
        ({ pagina, filas } = paginar(todos, pagina, f.limite));
        total = todos.length;
      } else {
        params.set('limit', f.limite);
        params.set('offset', pagina * f.limite);
        const data = await getJson(`/api/historico/usuarios?${params}`);
        total = Number(data.total) || 0;
        filas = data.usuarios || [];
      }
      if (peticion !== padron.peticion) return;
      Object.assign(padron, { pagina, total, cargada: true });
      pintarPadron(filas, Boolean(f.q || f.plataforma || f.artista));
    } catch (err) {
      if (peticion !== padron.peticion) return;
      console.error('Error al cargar el padrón', err);
      Object.assign(padron, { filas: [], total: 0 });
      filaVacia('tablaPadron', 9, 'No se pudo cargar el padrón. Intenta de nuevo.');
      renderPager('pagPadron', padron, f.limite);
    } finally {
      if (peticion === padron.peticion) setBusy('wrapPadron', false);
    }
  }

  function padronPrimeraPagina() {
    padron.pagina = 0;
    cargarPadron();
  }
  filtrosPadron.buscar?.addEventListener('input', debounce(padronPrimeraPagina, 300));
  ['plataforma', 'artista', 'filas'].forEach((k) => filtrosPadron[k]?.addEventListener('change', padronPrimeraPagina));
  $('pLimpiar')?.addEventListener('click', () => {
    if (filtrosPadron.buscar) filtrosPadron.buscar.value = '';
    if (filtrosPadron.plataforma) filtrosPadron.plataforma.value = '';
    if (filtrosPadron.artista) filtrosPadron.artista.value = '';
    if (filtrosPadron.filas) filtrosPadron.filas.value = '25';
    padronPrimeraPagina();
    filtrosPadron.buscar?.focus();
  });
  $('pagPadronAnterior')?.addEventListener('click', () => { padron.pagina = Math.max(0, padron.pagina - 1); cargarPadron(); });
  $('pagPadronSiguiente')?.addEventListener('click', () => { padron.pagina += 1; cargarPadron(); });

  const MOTIVOS_RECHAZO = { FK_USUARIO_INEXISTENTE: 'El usuario de origen no existe en el padrón' };
  const filtrosCuar = { motivo: $('cMotivo'), filas: $('cFilas') };
  const cuar = { pagina: 0, total: 0, filas: [], peticion: 0, cargada: false };

  function etiquetaMotivo(m) {
    const codigo = escapeHtml(m || 'SIN_MOTIVO');
    const ayuda = MOTIVOS_RECHAZO[m] ? ` title="${escapeHtml(MOTIVOS_RECHAZO[m])}"` : '';
    return `<span class="code-tag"${ayuda}>${codigo}</span>`;
  }

  function pintarCuarentena(data, filtrado) {
    cuar.filas = data.registros || [];
    const porMotivo = data.por_motivo || [];
    llenarSelect(filtrosCuar.motivo, porMotivo.filter(m => m.motivo_rechazo).map(m => ({ valor: m.motivo_rechazo, texto: `${m.motivo_rechazo} (${fmtEntero(m.total)})` })));
    const resumen = $('cuarResumen');
    if (resumen) {
      resumen.innerHTML = porMotivo.map(m => `<span class="summary-chip">${etiquetaMotivo(m.motivo_rechazo)}<span class="mono">${fmtEntero(m.total)}</span>${MOTIVOS_RECHAZO[m.motivo_rechazo] ? `<span class="muted">${escapeHtml(MOTIVOS_RECHAZO[m.motivo_rechazo])}</span>` : ''}</span>`).join('');
    }
    const tbody = $('tablaCuarentena');
    if (tbody) {
      tbody.innerHTML = cuar.filas.length
        ? cuar.filas.map(r => `<tr>
            <td class="mono">${escapeHtml(r.id_interaccion || '')}</td>
            <td class="mono">${escapeHtml(r.id_usuario_origen || '—')}</td>
            <td class="mono">${escapeHtml(fmtFechaHora(r.timestamp_origen))}</td>
            <td><span class="pill">${escapeHtml(etiqueta(r.canal))}</span></td>
            <td>${escapeHtml(etiqueta(r.tipo))}</td>
            <td>${escapeHtml(etiqueta(r.tema))}</td>
            <td>${etiquetaMotivo(r.motivo_rechazo)}</td>
          </tr>`).join('')
        : `<tr><td colspan="7" class="empty">${filtrado ? 'Ningún registro coincide con el motivo elegido.' : 'No hay registros en cuarentena.'}</td></tr>`;
    }
    renderPager('pagCuar', cuar, Number(filtrosCuar.filas?.value) || 25);
  }

  async function cargarCuarentena() {
    const motivo = filtrosCuar.motivo?.value || '';
    const limite = Number(filtrosCuar.filas?.value) || 25;
    const peticion = ++cuar.peticion;
    setBusy('wrapCuarentena', true);
    try {
      const params = new URLSearchParams({ limit: limite, offset: cuar.pagina * limite });
      if (motivo) params.set('motivo', motivo);
      const data = await getJson(`/api/historico/cuarentena?${params}`);
      if (peticion !== cuar.peticion) return;
      Object.assign(cuar, { total: Number(data.total) || 0, cargada: true });
      pintarCuarentena(data, Boolean(motivo));
    } catch (err) {
      if (peticion !== cuar.peticion) return;
      console.error('Error al cargar la cuarentena', err);
      Object.assign(cuar, { filas: [], total: 0 });
      filaVacia('tablaCuarentena', 7, 'No se pudo cargar la cuarentena. Intenta de nuevo.');
      renderPager('pagCuar', cuar, limite);
    } finally {
      if (peticion === cuar.peticion) setBusy('wrapCuarentena', false);
    }
  }

  function cuarPrimeraPagina() {
    cuar.pagina = 0;
    cargarCuarentena();
  }
  ['motivo', 'filas'].forEach((k) => filtrosCuar[k]?.addEventListener('change', cuarPrimeraPagina));
  $('cLimpiar')?.addEventListener('click', () => {
    if (filtrosCuar.motivo) filtrosCuar.motivo.value = '';
    if (filtrosCuar.filas) filtrosCuar.filas.value = '25';
    cuarPrimeraPagina();
  });
  $('pagCuarAnterior')?.addEventListener('click', () => { cuar.pagina = Math.max(0, cuar.pagina - 1); cargarCuarentena(); });
  $('pagCuarSiguiente')?.addEventListener('click', () => { cuar.pagina += 1; cargarCuarentena(); });

  // Pestañas del explorador (patrón WAI-ARIA: flechas, Inicio y Fin mueven la selección).
  const tabsExplorador = Array.from(document.querySelectorAll('.segmented [role="tab"]'));
  function activarPestana(tab, enfocar = false) {
    tabsExplorador.forEach((t) => {
      const activa = t === tab;
      t.setAttribute('aria-selected', String(activa));
      t.tabIndex = activa ? 0 : -1;
      const panel = $(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !activa;
    });
    if (enfocar) tab.focus();
    if (tab.id === 'tabCuarentena' && !cuar.cargada) cargarCuarentena();
    if (tab.id === 'tabPadron' && !padron.cargada) cargarPadron();
  }
  tabsExplorador.forEach((tab, idx) => {
    tab.addEventListener('click', () => activarPestana(tab));
    tab.addEventListener('keydown', (ev) => {
      const destino = {
        ArrowRight: tabsExplorador[(idx + 1) % tabsExplorador.length],
        ArrowLeft: tabsExplorador[(idx - 1 + tabsExplorador.length) % tabsExplorador.length],
        Home: tabsExplorador[0],
        End: tabsExplorador[tabsExplorador.length - 1]
      }[ev.key];
      if (!destino) return;
      ev.preventDefault();
      activarPestana(destino, true);
    });
  });

  async function init() {
    syncThemeToggle();
    refreshChartThemes();
    connectWs();
    await Promise.allSettled([verifySession(), loadHistoricos(), cargarPadron(), cargarInteracciones()]);
  }

  init();
})();