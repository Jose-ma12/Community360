(() => {
  const API_BASE = '';
  const token = sessionStorage.getItem('c360_token');
  const user = JSON.parse(sessionStorage.getItem('c360_user') || '{}');

  if (!token) {
    window.location.href = '/static/login.html';
    return;
  }

  const $ = (id) => document.getElementById(id);
  const wsDot = $('wsDot');
  const wsStatus = $('wsStatus');
  const wsLog = $('wsLog');
  const logoutBtn = $('logoutBtn');
  $('userInfo').textContent = user.nombre_completo ? `${user.nombre_completo} (${user.username || ''})` : 'Monitoreo en tiempo real e históricos';

  const charts = {};
  let ws = null;
  let wsReconnectTimer = null;
  let wsPingTimer = null;
  let manualLogout = false;

  function authHeaders() {
    return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
  }

  async function apiFetch(path, options = {}) {
    const resp = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { ...(options.headers || {}), ...authHeaders() }
    });
    if (resp.status === 401) {
      sessionStorage.removeItem('c360_token');
      sessionStorage.removeItem('c360_user');
      window.location.href = '/static/login.html';
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

  logoutBtn.addEventListener('click', async () => {
    manualLogout = true;
    try { await apiFetch('/api/auth/logout', { method: 'POST' }); } catch {}
    sessionStorage.removeItem('c360_token');
    sessionStorage.removeItem('c360_user');
    if (ws) try { ws.close(); } catch {}
    window.location.href = '/static/login.html';
  });

  function setWsStatus(connected, text) {
    wsDot.className = `dot ${connected ? 'ok' : 'bad'}`;
    wsStatus.textContent = text;
  }

  function logWsEvent(evt) {
    if (wsLog.querySelector('.empty')) wsLog.innerHTML = '';
    const item = document.createElement('div');
    item.className = 'log-item';
    const ts = evt.timestamp_utc || new Date().toISOString();
    const tipo = evt.evento || evt.type || 'evento';
    const actor = evt.actor ? ` · ${evt.actor}` : '';
    const detail = evt.interaccion?.id_interaccion ? ` · ${evt.interaccion.id_interaccion}` : '';
    item.innerHTML = `<div class="meta"><span>${tipo}${actor}${detail}</span><span>${ts}</span></div><div>${summarizeEvent(evt)}</div>`;
    wsLog.prepend(item);
    while (wsLog.children.length > 30) wsLog.removeChild(wsLog.lastChild);
  }

  function summarizeEvent(evt) {
    if (evt.type === 'pong') return 'Pong recibido.';
    if (evt.metricas) {
      const m = evt.metricas;
      return `N=${m.total_interacciones} · S̄=${fmtNum(m.sentimiento_promedio)} · R̄=${fmtNum(m.reacciones_promedio)} · L̄=${fmtNum(m.longitud_media_texto)}`;
    }
    return 'Evento recibido.';
  }

  function fmtNum(v) {
    const n = Number(v || 0);
    if (Number.isInteger(n)) return n.toString();
    return n.toFixed(4).replace(/\.?0+$/, '');
  }

  function updateKpis(metricas) {
    $('kpiTotal').textContent = fmtNum(metricas.total_interacciones);
    $('kpiSentimiento').textContent = fmtNum(metricas.sentimiento_promedio);
    $('kpiReacciones').textContent = fmtNum(metricas.reacciones_promedio);
    $('kpiLongitud').textContent = fmtNum(metricas.longitud_media_texto);
    $('ultimaActualizacion').textContent = `Última actualización: ${metricas.ultima_actualizacion || '—'}`;
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

  const palette = ['#1f6feb', '#00a389', '#f0a202', '#d94f4f', '#7c5cff', '#2bb3c0', '#ef6c9f', '#6b7280', '#4aa3ff', '#90be6d'];

  function updateRealtimeCharts(metricas) {
    const canalLabels = Object.keys(metricas.por_canal || {});
    const canalValues = canalLabels.map(k => metricas.por_canal[k]);
    ensureChart('chartCanal', {
      type: 'bar',
      data: { labels: canalLabels, datasets: [{ label: 'Interacciones', data: canalValues, backgroundColor: '#1f6feb', borderRadius: 8 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
    });

    const temaLabels = Object.keys(metricas.por_tema || {});
    const temaValues = temaLabels.map(k => metricas.por_tema[k]);
    ensureChart('chartTema', {
      type: 'doughnut',
      data: { labels: temaLabels, datasets: [{ data: temaValues, backgroundColor: temaLabels.map((_, i) => palette[i % palette.length]) }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } }
    });
  }

  function renderHeatmap(heatmap) {
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
        const bg = `rgba(31,111,235,${0.08 + ratio * 0.82})`;
        const color = ratio > 0.55 ? '#fff' : '#10284a';
        html += `<td class="cell" style="background:${bg};color:${color}">${v}</td>`;
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    $('heatmapContainer').innerHTML = html;
  }

  function renderUltimas(interacciones) {
    const tbody = $('tablaInteracciones');
    if (!interacciones || !interacciones.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">Sin interacciones disponibles.</td></tr>';
      return;
    }
    tbody.innerHTML = interacciones.map(i => {
      const texto = i.tipo === 'reaccion' ? '<span class="muted">— reacción —</span>' : escapeHtml((i.texto || '').slice(0, 80));
      return `<tr>
        <td>${escapeHtml(i.id_interaccion || '')}</td>
        <td>${escapeHtml(i.id_usuario || '')}</td>
        <td>${escapeHtml(i.timestamp || '')}</td>
        <td><span class="pill">${escapeHtml(i.canal || '')}</span></td>
        <td>${escapeHtml(i.tipo || '')}</td>
        <td>${escapeHtml(i.tema || '')}</td>
        <td>${fmtNum(i.sentimiento)}</td>
        <td>${fmtNum(i.reacciones_recibidas)}</td>
        <td>${texto}</td>
      </tr>`;
    }).join('');
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
    setWsStatus(false, 'WS: conectando...');
    try { ws = new WebSocket(wsUrl); } catch { scheduleReconnect(); return; }

    ws.onopen = () => {
      setWsStatus(true, 'WS: en vivo');
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
      if (msg.error) console.error(msg.error);
      if (msg.metricas && !msg.error) applyMetricas(msg.metricas);
      if (['conexion_inicial', 'interaccion_creada', 'interaccion_actualizada', 'interaccion_eliminada', 'conexion'].includes(msg.type || msg.evento)) {
        logWsEvent(msg);
      }
    };

    ws.onclose = (ev) => {
      setWsStatus(false, 'WS: desconectado');
      console.warn(`WebSocket cerrado (código ${ev.code})${ev.reason ? `: ${ev.reason}` : ''}`);
      if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
      if (!manualLogout) scheduleReconnect();
    };

    ws.onerror = () => {
      setWsStatus(false, 'WS: error');
      console.error('Error de conexión WebSocket', wsUrl);
    };
  }

  function scheduleReconnect() {
    if (wsReconnectTimer) return;
    wsReconnectTimer = setTimeout(() => { wsReconnectTimer = null; connectWs(); }, 3000);
  }

  async function loadHistoricos() {
    try {
      const resp = await apiFetch('/api/historico/dashboard');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (data.error_firestore) console.error(data.error_firestore);
      renderHistoricos(data);
      const metricasHistoricas = data.metricas_tiempo_real || data.metricas_firestore;
      if (metricasHistoricas && !data.error_firestore) applyMetricas(metricasHistoricas);
    } catch (err) {
      console.error('Error al cargar los históricos', err);
    }
  }

  function renderHistoricos(data) {
    const r = data.resumen_usuarios || {};
    $('hTotalUsuarios').textContent = fmtNum(r.total_usuarios);
    $('hConConsent').textContent = fmtNum(r.con_consentimiento_geo);
    $('hSinConsent').textContent = fmtNum(r.sin_consentimiento_geo);
    $('hCuarentena').textContent = fmtNum(data.interacciones_cuarentena);
    if (data.privacidad?.filtro_top_ciudades) {
      $('bannerPrivacidad').textContent = `Verificación de privacidad: ${data.privacidad.filtro_top_ciudades} (regla ${data.privacidad.regla || 'sin_datos_geo'}).`;
    }

    const plat = data.por_plataforma_principal || [];
    ensureChart('chartPlataforma', {
      type: 'bar',
      data: { labels: plat.map(x => x.plataforma_principal || 'N/D'), datasets: [{ label: 'Usuarios', data: plat.map(x => x.total), backgroundColor: '#00a389', borderRadius: 8 }] },
      options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } }
    });

    const riesgo = data.riesgo_abandono || {};
    const riesgoLabels = ['Riesgo alto', 'Riesgo medio', 'Activos'];
    ensureChart('chartRiesgo', {
      type: 'doughnut',
      data: { labels: riesgoLabels, datasets: [{ data: riesgoLabels.map(l => riesgo[l] || 0), backgroundColor: ['#d94f4f', '#f0a202', '#20b26b'] }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } }
    });

    const ciudades = data.top_ciudades_autorizadas || [];
    ensureChart('chartCiudades', {
      type: 'bar',
      data: {
        labels: ciudades.map(c => `${c.ciudad || 'N/D'}${c.estado ? ', ' + c.estado : ''}`),
        datasets: [{ label: 'Usuarios', data: ciudades.map(c => c.total), backgroundColor: '#1f6feb', borderRadius: 8 }]
      },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
    });
  }

  async function init() {
    connectWs();
    await verifySession();
    await loadHistoricos();
  }

  init();
})();