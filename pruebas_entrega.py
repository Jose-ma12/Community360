"""Pruebas automáticas de Community360 (fases 3 a 6). NO crean, editan ni borran datos.

Uso (con el servidor ya corriendo en otra terminal):
    python pruebas_entrega.py

Lee ADMIN_USERNAME y ADMIN_PASSWORD del .env (si falta la contraseña la pide en pantalla).
Nunca imprime la contraseña ni el token.
"""
import asyncio
import getpass
import json
import os
import sys

import requests
import websockets
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("C360_BASE", "http://127.0.0.1:8000").rstrip("/")
WS_URL = BASE.replace("http", "ws", 1) + "/ws"
USUARIO = os.getenv("ADMIN_USERNAME", "admin_c360")
CLAVE = os.getenv("ADMIN_PASSWORD") or getpass.getpass("Contraseña del administrador: ")

resultados = []


def prueba(fase, nombre, condicion, detalle=""):
    ok = bool(condicion)
    resultados.append(ok)
    extra = f"  -> {detalle}" if detalle and not ok else ""
    print(f"[{'OK   ' if ok else 'FALLA'}] {fase} | {nombre}{extra}")


def get(ruta, token=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.get(BASE + ruta, headers=headers, timeout=30, allow_redirects=False, **kw)


def enviar(metodo, ruta, token=None, cuerpo=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.request(metodo, BASE + ruta, headers=headers, json=cuerpo, timeout=30, allow_redirects=False)


async def ws_primer_evento(mensaje_auth, espera=8):
    """Devuelve ('mensaje', dict) si llega un mensaje o ('cierre', código) si el servidor cierra."""
    async with websockets.connect(WS_URL, open_timeout=10) as ws:
        if mensaje_auth is not None:
            await ws.send(json.dumps(mensaje_auth))
        try:
            dato = await asyncio.wait_for(ws.recv(), timeout=espera)
        except websockets.ConnectionClosed:
            return "cierre", ws.close_code
        except asyncio.TimeoutError:
            return "nada", None
        msg = json.loads(dato)
        if mensaje_auth is not None and msg.get("type") == "conexion_inicial":
            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=espera))
            msg["_pong"] = pong.get("type")
        return "mensaje", msg


def main():
    print(f"Probando {BASE}\n")
    invalido = "token.falso.123"

    # ---------------- Fase 3: autenticación ----------------
    r = get("/")
    prueba("F3", "La raíz manda al login", r.status_code in (302, 307) and "login" in r.headers.get("location", ""), r.status_code)
    r = get("/dashboard")
    prueba("F3", "Dashboard sin sesión redirige al login (303)", r.status_code == 303 and "login" in r.headers.get("location", ""), r.status_code)
    prueba("F3", "API sin token -> 401", get("/api/interacciones").status_code == 401)
    prueba("F3", "API con token falso -> 401", get("/api/interacciones", invalido).status_code == 401)
    r = enviar("POST", "/api/auth/login", cuerpo={"username": "usuario_que_no_existe", "password": "x"})
    prueba("F3", "Usuario inexistente -> 401 'Credenciales inválidas'", r.status_code == 401 and r.json().get("detail") == "Credenciales inválidas", r.status_code)
    r = enviar("POST", "/api/auth/login", cuerpo={"username": USUARIO, "password": "contraseña-incorrecta"})
    prueba("F3", "Contraseña incorrecta -> 401 con el MISMO mensaje", r.status_code == 401 and r.json().get("detail") == "Credenciales inválidas", r.status_code)
    prueba("F3", "Login sin campos -> 422", enviar("POST", "/api/auth/login", cuerpo={}).status_code == 422)
    codigos = [enviar("POST", "/api/auth/login", cuerpo={"username": "prueba_bloqueo", "password": "x"}).status_code for _ in range(6)]
    prueba("F3", "6 intentos fallidos -> 429 (límite de intentos)", codigos[-1] == 429, codigos)

    r = enviar("POST", "/api/auth/login", cuerpo={"username": USUARIO, "password": CLAVE})
    if r.status_code != 200:
        prueba("F3", "Login correcto -> 200", False, f"HTTP {r.status_code}. Revisa ADMIN_USERNAME/ADMIN_PASSWORD en .env")
        return resumen()
    datos = r.json()
    token = datos.get("access_token", "")
    cookie = r.headers.get("set-cookie", "").lower()
    prueba("F3", "Login correcto -> 200 con token Bearer", bool(token) and datos.get("token_type") == "Bearer")
    prueba("F3", "La respuesta del login no incluye el hash de la contraseña", "password" not in json.dumps(datos).lower())
    prueba("F3", "Cookie de sesión HttpOnly y SameSite=Strict", "httponly" in cookie and "samesite=strict" in cookie)
    prueba("F3", "Verificar sesión -> 200", get("/api/auth/verify", token).status_code == 200)
    r = get("/dashboard", cookies={"c360_session": token})
    prueba("F3", "Dashboard con sesión -> 200 HTML", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), r.status_code)

    # Nada sensible expuesto al navegador
    for ruta in ("/.env", "/static/.env", "/firebase_credentials.json", "/static/firebase_credentials.json", "/main.py", "/static/../.env"):
        prueba("F3", f"No se puede descargar {ruta}", get(ruta).status_code in (404, 400, 307, 302), get(ruta).status_code)
    frontend = "".join(get(p).text for p in ("/static/login.html", "/static/login.js", "/static/app.js", "/static/tema.js"))
    secretos = [s for s in ("CLOUDFLARE_API_TOKEN", "JWT_SECRET_KEY", "private_key", "api.cloudflare.com", "ADMIN_PASSWORD") if s in frontend]
    prueba("F3", "El frontend no contiene credenciales ni cadenas de conexión", not secretos, secretos)
    prueba("F3", "/docs y /openapi.json apagados", get("/docs").status_code == 404 and get("/openapi.json").status_code == 404)
    csp = get("/static/login.html").headers.get("content-security-policy", "")
    script_src = next((p for p in csp.split(";") if p.strip().startswith("script-src")), "")
    prueba("F3", "CSP activa y sin scripts en línea", bool(csp) and "unsafe-inline" not in script_src, script_src)
    r = requests.options(BASE + "/api/interacciones", headers={"Origin": "https://sitio-malicioso.com", "Access-Control-Request-Method": "GET"}, timeout=30)
    prueba("F3", "CORS rechaza sitios desconocidos", "access-control-allow-origin" not in {k.lower() for k in r.headers})

    # ---------------- Fase 4: API REST ----------------
    r = get("/api/interacciones?limit=5", token)
    lista = r.json() if r.status_code == 200 else {}
    prueba("F4", "Listar interacciones -> 200 con paginación", r.status_code == 200 and lista.get("total", 0) > 0 and len(lista.get("interacciones", [])) <= 5, r.status_code)
    geo = [i for i in lista.get("interacciones", []) if any(k in i for k in ("ciudad", "latitud", "longitud", "pais", "estado"))]
    prueba("F4", "Las interacciones no guardan datos geográficos", not geo)
    prueba("F4", "limit=0 -> 422", get("/api/interacciones?limit=0", token).status_code == 422)
    prueba("F4", "limit=1000 -> 422", get("/api/interacciones?limit=1000", token).status_code == 422)
    prueba("F4", "Id con formato inválido -> 400", get("/api/interacciones/__x__", token).status_code == 400)
    prueba("F4", "Id inexistente -> 404", get("/api/interacciones/I99999999", token).status_code == 404)

    base = {"id_interaccion": "I99999998", "id_usuario": "U000001", "timestamp": "2026-11-20T18:00:00Z", "canal": "Telegram",
            "tipo": "mensaje", "texto": "prueba", "tema": "arte_comisiones", "sentimiento": 0.5, "idioma": "es",
            "dispositivo": "Android", "reacciones_recibidas": 1, "etiquetas": []}
    malos = {
        "canal inválido": {"canal": "MySpace"}, "tipo inválido": {"tipo": "encuesta"}, "tema inválido": {"tema": "Tecnología"},
        "sentimiento fuera de rango": {"sentimiento": 2}, "reacciones negativas": {"reacciones_recibidas": -1},
        "fecha sin zona horaria": {"timestamp": "2026-11-20T18:00:00"}, "fecha en el futuro": {"timestamp": "2030-01-01T00:00:00Z"},
        "idioma inválido": {"idioma": "Español"}, "mensaje sin texto": {"texto": ""}, "texto de 2001 caracteres": {"texto": "a" * 2001},
        "id con formato inválido": {"id_interaccion": "X1"}, "campo no permitido": {"ciudad": "Mérida"},
    }
    for nombre, cambio in malos.items():
        prueba("F4", f"POST con {nombre} -> 422", enviar("POST", "/api/interacciones", token, {**base, **cambio}).status_code == 422)
    prueba("F4", "POST sin token -> 401", enviar("POST", "/api/interacciones", None, base).status_code == 401)
    r = enviar("POST", "/api/interacciones", token, {**base, "id_usuario": "U99999999"})
    prueba("F4", "POST con usuario inexistente en D1 -> 400 (no se guarda)", r.status_code == 400, r.status_code)
    prueba("F4", "PUT sin campos -> 400", enviar("PUT", "/api/interacciones/I99999999", token, {}).status_code == 400)
    prueba("F4", "PUT con dato inválido -> 422", enviar("PUT", "/api/interacciones/I99999999", token, {"tema": "Tecnología"}).status_code == 422)
    prueba("F4", "PUT a id inexistente -> 404", enviar("PUT", "/api/interacciones/I99999999", token, {"tema": "musica"}).status_code == 404)
    prueba("F4", "DELETE a id inexistente -> 404", enviar("DELETE", "/api/interacciones/I99999999", token).status_code == 404)

    r = get("/api/historico/dashboard", token)
    hist = r.json() if r.status_code == 200 else {}
    prueba("F4", "Histórico del dashboard -> 200 desde D1", r.status_code == 200 and hist.get("resumen_usuarios", {}).get("total_usuarios", 0) > 0, r.status_code)
    prueba("F4", "El histórico no expone SQL ni errores técnicos", "SELECT" not in r.text and "Traceback" not in r.text)
    r = get("/api/historico/usuarios?limit=200", token)
    usuarios = r.json().get("usuarios", []) if r.status_code == 200 else []
    fuga = [u["id_usuario"] for u in usuarios if not u.get("consentimiento_datos") and any(u.get(k) for k in ("ciudad", "estado", "pais"))]
    prueba("F4", "Padrón: sin consentimiento NO muestra ubicación", r.status_code == 200 and usuarios and not fuga, fuga[:5])
    prueba("F4", "Usuario con id inválido -> 400", get("/api/historico/usuarios/abc", token).status_code == 400)
    r = get("/api/historico/cuarentena?limit=5", token)
    prueba("F4", "Cuarentena consultable (registros rechazados por el ETL)", r.status_code == 200 and r.json().get("total", 0) > 0, r.status_code)
    r = get("/api/health")
    prueba("F4", "/api/health -> 200 (D1 y Firestore conectados)", r.status_code == 200, r.text)

    # ---------------- Fase 5: WebSockets ----------------
    tipo, valor = asyncio.run(ws_primer_evento(None))
    prueba("F5", "WebSocket sin autenticación se cierra (1008)", tipo == "cierre" and valor == 1008, (tipo, valor))
    tipo, valor = asyncio.run(ws_primer_evento({"type": "auth", "token": invalido}))
    prueba("F5", "WebSocket con token falso se cierra (1008)", tipo == "cierre" and valor == 1008, (tipo, valor))
    tipo, valor = asyncio.run(ws_primer_evento({"type": "auth", "token": token}))
    ok_ws = tipo == "mensaje" and valor.get("type") == "conexion_inicial"
    prueba("F5", "WebSocket autenticado recibe 'conexion_inicial' con métricas", ok_ws and "metricas" in valor, tipo)
    prueba("F5", "Comunicación bidireccional: ping -> pong", ok_ws and valor.get("_pong") == "pong")

    # ---------------- Fase 6: dashboard ----------------
    if ok_ws:
        m_ws = valor["metricas"]
        m_rest = hist.get("metricas_tiempo_real", {})
        prueba("F6", "Indicadores en vivo = indicadores por REST (mismo total)", m_ws.get("total_interacciones") == m_rest.get("total_interacciones"),
               (m_ws.get("total_interacciones"), m_rest.get("total_interacciones")))
        claves = ("total_interacciones", "sentimiento_promedio", "reacciones_promedio", "longitud_media_texto", "por_canal", "por_tema", "heatmap_actividad")
        prueba("F6", "Las métricas traen KPI, barras, dona y mapa de calor", all(k in m_ws for k in claves), [k for k in claves if k not in m_ws])
    html = get("/dashboard", cookies={"c360_session": token}).text.lower()
    prueba("F6", "El dashboard NO tiene formularios de alta/edición/borrado", "<form" not in html and "method=\"post\"" not in html)
    prueba("F6", "El dashboard es responsivo (meta viewport)", 'name="viewport"' in html)

    # ---------------- Cierre de sesión (al final) ----------------
    r = enviar("POST", "/api/auth/logout", token)
    prueba("F3", "Cerrar sesión -> 200", r.status_code == 200, r.status_code)
    prueba("F3", "El token revocado ya no sirve -> 401", get("/api/auth/verify", token).status_code == 401)
    prueba("F3", "Dashboard con la sesión cerrada -> redirige al login", get("/dashboard", cookies={"c360_session": token}).status_code == 303)
    tipo, valor = asyncio.run(ws_primer_evento({"type": "auth", "token": token}))
    prueba("F5", "WebSocket con token revocado se cierra (1008)", tipo == "cierre" and valor == 1008, (tipo, valor))
    return resumen()


def resumen():
    total, bien = len(resultados), sum(resultados)
    print(f"\nResultado: {bien}/{total} pruebas correctas")
    return 0 if bien == total else 1


if __name__ == "__main__":
    sys.exit(main())
