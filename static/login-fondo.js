/* Community360 · Fondo animado del login: panel de actividad */
(function () {
  'use strict';

  const PASO = 22;                // separación entre puntos en px
  const RADIO_CURSOR = 160;       // alcance de la luz del cursor en px
  const ENCENDIDOS_POR_SEG = 0.9; // puntos que se encienden por segundo por cada 100 puntos

  function leerColores() {
    const claro = document.documentElement.getAttribute('data-theme') === 'light';
    return claro
      ? { base: '52, 84, 128', alt: '70, 110, 95', minimo: 0.10 }
      : { base: '143, 179, 224', alt: '126, 154, 140', minimo: 0.07 };
  }

  class FondoActividad {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.mouse = { x: -9999, y: -9999 };
      this.puntos = [];
      this.anterior = 0;
      this.acumulado = 0;
      this.activo = false;
      this.reducido = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      this.colores = leerColores();
      this.cuadro = this.cuadro.bind(this);
      this.medir();
    }

    medir() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.W = window.innerWidth;
      this.H = window.innerHeight;
      this.canvas.width = Math.round(this.W * dpr);
      this.canvas.height = Math.round(this.H * dpr);
      this.canvas.style.width = this.W + 'px';
      this.canvas.style.height = this.H + 'px';
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.puntos = [];
      for (let y = PASO / 2; y < this.H; y += PASO) {
        for (let x = PASO / 2; x < this.W; x += PASO) {
          this.puntos.push({ x: x, y: y, v: 0, meta: 0, alt: false });
        }
      }
      if (this.reducido) this.dibujar(0);
    }

    encenderAlAzar(n) {
      for (let i = 0; i < n; i++) {
        const p = this.puntos[(Math.random() * this.puntos.length) | 0];
        if (!p) return;
        p.alt = Math.random() < 0.12;
        p.meta = Math.random() < 0.85 ? 0.55 : 1;
      }
    }

    dibujar(t) {
      const ctx = this.ctx;
      const c = this.colores;
      ctx.clearRect(0, 0, this.W, this.H);
      const filas = Math.max(1, Math.ceil(this.H / PASO));
      const fila = this.reducido ? -1 : Math.floor((t * 0.6) % filas);
      for (const p of this.puntos) {
        p.v += (p.meta - p.v) * 0.04;
        if (p.meta > 0 && p.v > p.meta - 0.02) p.meta = 0;
        const d = Math.hypot(p.x - this.mouse.x, p.y - this.mouse.y);
        const cerca = d < RADIO_CURSOR ? 1 - d / RADIO_CURSOR : 0;
        let a = c.minimo + p.v * 0.55 + cerca * 0.35;
        if (Math.floor(p.y / PASO) === fila) a += 0.05;
        ctx.fillStyle = 'rgba(' + (p.alt ? c.alt : c.base) + ', ' + Math.min(a, 1).toFixed(3) + ')';
        ctx.fillRect(p.x - 1, p.y - 1, 2, 2);
      }
    }

    cuadro(ahora) {
      if (!this.activo) return;
      const t = ahora / 1000;
      const dt = this.anterior ? Math.min(t - this.anterior, 0.1) : 0;
      this.anterior = t;
      this.acumulado += dt * ENCENDIDOS_POR_SEG * (this.puntos.length / 100);
      const n = Math.floor(this.acumulado);
      if (n > 0) {
        this.encenderAlAzar(n);
        this.acumulado -= n;
      }
      this.dibujar(t);
      requestAnimationFrame(this.cuadro);
    }

    arrancar() {
      if (this.reducido || this.activo) return;
      this.activo = true;
      this.anterior = 0;
      requestAnimationFrame(this.cuadro);
    }

    detener() {
      this.activo = false;
    }
  }

  function iniciar() {
    const canvas = document.getElementById('fondoActividad');
    if (!canvas || !canvas.getContext) return;
    const fondo = new FondoActividad(canvas);
    let espera = null;

    window.addEventListener('resize', function () {
      clearTimeout(espera);
      espera = setTimeout(function () { fondo.medir(); }, 150);
    });
    window.addEventListener('mousemove', function (e) {
      fondo.mouse.x = e.clientX;
      fondo.mouse.y = e.clientY;
      if (fondo.reducido) fondo.dibujar(0);
    }, { passive: true });
    document.addEventListener('mouseleave', function () {
      fondo.mouse.x = -9999;
      fondo.mouse.y = -9999;
    });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) fondo.detener();
      else fondo.arrancar();
    });
    new MutationObserver(function () {
      fondo.colores = leerColores();
      if (fondo.reducido) fondo.dibujar(0);
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });

    fondo.arrancar();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
  else iniciar();
})();
