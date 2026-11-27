(function () {
  // Misma clave que usa el dashboard (THEME_KEY en app.js): el tema elegido aquí se conserva al entrar.
  var CLAVE = 'c360_theme';
  var btn = document.getElementById('themeToggleBtn');
  if (!btn) return;
  var label = btn.querySelector('.theme-label');
  function aplicarTema(tema) {
    tema = tema === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', tema);
    try { localStorage.setItem(CLAVE, tema); } catch (e) {}
    var light = tema === 'light';
    if (label) label.textContent = light ? 'Modo oscuro' : 'Modo claro';
    btn.setAttribute('aria-label', light ? 'Activar modo oscuro' : 'Activar modo claro');
    btn.setAttribute('aria-pressed', String(light));
  }
  var temaInicial = 'dark';
  try { temaInicial = localStorage.getItem(CLAVE) || 'dark'; } catch (e) {}
  aplicarTema(temaInicial);
  btn.addEventListener('click', function () {
    var actual = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    aplicarTema(actual === 'light' ? 'dark' : 'light');
  });
})();

(function () {
  var toggle = document.getElementById('passwordToggle');
  var input = document.getElementById('password');
  if (!toggle || !input) return;
  toggle.addEventListener('click', function () {
    var visible = input.type === 'password';
    input.type = visible ? 'text' : 'password';
    toggle.classList.toggle('is-visible', visible);
    toggle.setAttribute('aria-pressed', String(visible));
    toggle.setAttribute('aria-label', visible ? 'Ocultar contraseña' : 'Mostrar contraseña');
    input.focus();
  });
})();

(function () {
  const API_BASE = '';
  // /dashboard manda aquí con ?sesion=expirada cuando no hay cookie de sesión válida:
  // se limpia la sesión local para no rebotar entre login y dashboard.
  const sesionExpirada = new URLSearchParams(window.location.search).has('sesion');
  if (sesionExpirada) {
    try { sessionStorage.removeItem('c360_token'); sessionStorage.removeItem('c360_user'); } catch (e) {}
  }
  const token = sesionExpirada ? null : sessionStorage.getItem('c360_token');
  if (token) {
    fetch(`${API_BASE}/api/auth/verify`, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(r => { if (r.ok) window.location.href = '/dashboard'; })
      .catch(() => {});
  }

  const form = document.getElementById('loginForm');
  const errorBox = document.getElementById('loginError');
  const btn = document.getElementById('loginBtn');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');
  if (!form || !errorBox || !btn || !usernameInput || !passwordInput) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorBox.className = 'error';
    errorBox.textContent = '';
    const username = usernameInput.value.trim();
    const password = passwordInput.value.trim();
    if (!username || !password) {
      errorBox.textContent = 'Usuario y contraseña son obligatorios.';
      errorBox.classList.add('show');
      return;
    }
    btn.disabled = true;
    btn.textContent = 'Verificando...';
    try {
      const resp = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        // Solo se muestran mensajes propios: nunca el texto técnico que pudiera mandar el servidor.
        let mensaje = 'No se pudo iniciar sesión. Intenta de nuevo en unos momentos.';
        if (resp.status === 401 || resp.status === 422) mensaje = 'Usuario o contraseña incorrectos.';
        if (resp.status === 429) mensaje = 'Demasiados intentos. Espera unos minutos e inténtalo de nuevo.';
        throw new Error(mensaje);
      }
      sessionStorage.setItem('c360_token', data.access_token);
      sessionStorage.setItem('c360_user', JSON.stringify(data.usuario || {}));
      window.location.href = '/dashboard';
    } catch (err) {
      errorBox.textContent = err instanceof TypeError ? 'No hay conexión con el servidor.' : (err.message || 'No se pudo iniciar sesión.');
      errorBox.classList.add('show');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Entrar';
    }
  });
})();
