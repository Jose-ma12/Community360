/* Community360 · Aplica el tema guardado antes de pintar la página (evita el parpadeo). */
(function () {
  var theme = 'dark';
  try { theme = localStorage.getItem('c360_theme') || 'dark'; } catch (e) {}
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
})();
