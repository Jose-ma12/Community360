import asyncio
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import firebase_admin
import jwt
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from firebase_admin import credentials, firestore
from google.api_core import exceptions as google_exceptions
from google.api_core.retry import Retry
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from metricas import (
    BLOQUES_HEATMAP,
    CANALES_VALIDOS,
    DIAS_HEATMAP,
    DISPOSITIVOS_VALIDOS,
    TEMAS_VALIDOS,
    TIPOS_VALIDOS,
    calcular_metricas,
    sanitizar_json,
    utc_now_iso,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

ID_INTERACCION_RE = re.compile(r"^I\d{1,9}$")
ID_USUARIO_RE = re.compile(r"^U\d{1,9}$")
MAX_TEXTO = 2000
MAX_ETIQUETAS = 20
MAX_LARGO_ETIQUETA = 40
IDIOMA_RE = re.compile(r"^[a-z]{2}$")
COLUMNAS_LOGIN_USUARIO = ("username", "nombre_completo", "password_hash", "rol", "activo")
COLUMNAS_PERFIL_USUARIO = ("username", "nombre_completo", "rol", "activo")

# Firestore: límites de espera para que un problema de red (proxy, antivirus, firewall)
# devuelva un error 503 en segundos en lugar de dejar el servidor colgado.
FIRESTORE_TIMEOUT = float(os.getenv("FIRESTORE_TIMEOUT_SECONDS", "15"))
FIRESTORE_RETRY = Retry(initial=0.5, maximum=2.0, multiplier=2.0, timeout=FIRESTORE_TIMEOUT)
FS = {"retry": FIRESTORE_RETRY, "timeout": FIRESTORE_TIMEOUT}

ESQUEMA_TOKENS_REVOCADOS = """
CREATE TABLE IF NOT EXISTS tokens_revocados (
    jti TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expira_epoch INTEGER NOT NULL,
    revocado_utc TEXT NOT NULL
)
"""

# Cookie de sesión: SOLO sirve para entregar la página del dashboard (GET /dashboard).
# La API y el WebSocket siguen usando el token Bearer, así que la cookie no abre la puerta a CSRF.
COOKIE_SESION = "c360_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"  # true cuando se sirva por HTTPS
RUTA_DASHBOARD_HTML = os.path.join(BASE_DIR, "templates", "dashboard.html")

WS_AUTH_TIMEOUT_S = 5

# Cabeceras de seguridad para todas las respuestas HTTP.
CABECERAS_SEGURIDAD = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}

security = HTTPBearer(auto_error=False)
TOKENS_REVOCADOS: set[str] = set()  # caché en memoria; la fuente de verdad es la tabla tokens_revocados de D1
FIRESTORE_DB = None


@asynccontextmanager
async def lifespan(_app):
    if len(os.getenv("JWT_SECRET_KEY", "")) < 32:
        print("[AVISO] JWT_SECRET_KEY tiene menos de 32 caracteres: genera uno con secrets.token_urlsafe(48)")
    try:
        await run_in_threadpool(ejecutar_d1, ESQUEMA_TOKENS_REVOCADOS)
    except Exception as exc:  # el servidor arranca aunque D1 no responda; se reintenta al cerrar sesión
        print(f"[AVISO] No se pudo verificar la tabla tokens_revocados en D1: {exc}")
    yield


# /docs y /openapi.json describen la API sin pedir sesión: apagados salvo que ENABLE_API_DOCS=true.
DOCS_HABILITADOS = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"
app = FastAPI(
    title="Community360 API",
    version="1.2.0",
    lifespan=lifespan,
    docs_url="/docs" if DOCS_HABILITADOS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if DOCS_HABILITADOS else None,
)
# La API usa encabezado Authorization (no cookies), así que no se necesitan credenciales CORS.
# Solo se aceptan peticiones de navegador desde los orígenes listados (Postman no usa CORS).
ORIGENES_PERMITIDOS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_PERMITIDOS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def cabeceras_de_seguridad(request, call_next):
    respuesta = await call_next(request)
    for nombre, valor in CABECERAS_SEGURIDAD.items():
        respuesta.headers.setdefault(nombre, valor)
    return respuesta


@app.exception_handler(google_exceptions.GoogleAPIError)
async def firestore_no_disponible(_request, exc):
    # El detalle técnico solo va a la consola del servidor; el cliente recibe un mensaje genérico.
    print(f"[Firestore] {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=503,
        content={"detail": "El servicio de datos no está disponible. Intenta de nuevo en unos momentos."},
    )


class GestorWebSockets:
    def __init__(self):
        self.activos: list[WebSocket] = []
        self.sesiones: dict[int, dict] = {}  # id(websocket) -> {"jti", "exp"} de la sesión que lo abrió
        self.lock = asyncio.Lock()

    async def conectar(self, websocket: WebSocket, sesion: dict | None = None):
        async with self.lock:
            self.activos.append(websocket)
            self.sesiones[id(websocket)] = sesion or {}

    async def desconectar(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.activos:
                self.activos.remove(websocket)
            self.sesiones.pop(id(websocket), None)

    def sesion_vigente(self, websocket: WebSocket) -> bool:
        return sesion_ws_vigente(self.sesiones.get(id(websocket)))

    async def broadcast(self, mensaje: dict):
        data = json.dumps(sanitizar_json(mensaje), ensure_ascii=False, default=str, allow_nan=False)
        async with self.lock:
            activos = list(self.activos)
        stale = []
        for ws in activos:
            # Una sesión que expiró o se cerró (logout) deja de recibir datos y se desconecta.
            if not self.sesion_vigente(ws):
                stale.append(ws)
                try:
                    await ws.close(code=1008)
                except Exception:
                    pass
                continue
            try:
                await ws.send_text(data)
            except Exception:
                stale.append(ws)
        for ws in stale:
            await self.desconectar(ws)


def sesion_ws_vigente(sesion: dict | None) -> bool:
    if not sesion or not sesion.get("jti"):
        return False
    if sesion["jti"] in TOKENS_REVOCADOS:
        return False
    exp = sesion.get("exp")
    return exp is None or time.time() < int(exp)


gestor_ws = GestorWebSockets()


# Límite de intentos fallidos de login por IP + usuario (en memoria): 5 fallos en 5 minutos → 429.
MAX_INTENTOS_LOGIN = int(os.getenv("LOGIN_MAX_INTENTOS", "5"))
VENTANA_LOGIN_S = int(os.getenv("LOGIN_VENTANA_SEGUNDOS", "300"))
_intentos_login: dict[str, list[float]] = {}
_intentos_lock = threading.Lock()


def _clave_intento(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "desconocida"
    return f"{ip}|{username.strip().lower()}"


def _verificar_bloqueo_login(clave: str) -> None:
    ahora = time.monotonic()
    with _intentos_lock:
        recientes = [t for t in _intentos_login.get(clave, []) if ahora - t < VENTANA_LOGIN_S]
        _intentos_login[clave] = recientes
        if len(recientes) >= MAX_INTENTOS_LOGIN:
            espera = int(VENTANA_LOGIN_S - (ahora - recientes[0])) + 1
            raise HTTPException(
                status_code=429,
                detail="Demasiados intentos fallidos. Intenta de nuevo más tarde.",
                headers={"Retry-After": str(espera)},
            )


def _registrar_fallo_login(clave: str) -> None:
    ahora = time.monotonic()
    with _intentos_lock:
        # Limpieza para que miles de usuarios inventados no llenen la memoria del servidor.
        if len(_intentos_login) > 5000:
            for k in [k for k, v in _intentos_login.items() if not v or ahora - v[-1] >= VENTANA_LOGIN_S]:
                _intentos_login.pop(k, None)
        _intentos_login.setdefault(clave, []).append(ahora)


# Hash de relleno: si el usuario no existe se compara contra este hash, así la respuesta tarda
# lo mismo que con un usuario real y no se puede adivinar qué usuarios existen midiendo el tiempo.
HASH_RELLENO = bcrypt.hashpw(b"community360-relleno", bcrypt.gensalt())


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=254)  # acepta el username o el correo
    password: str = Field(..., min_length=1, max_length=128)


def _validar_enum(valor, permitidos, campo):
    if valor is not None and valor not in permitidos:
        raise ValueError(f"{campo} inválido. Permitidos: {permitidos}")
    return valor


def _validar_idioma(valor):
    if valor is not None and not IDIOMA_RE.match(valor):
        raise ValueError("idioma debe ser un código ISO-639-1 de dos letras minúsculas (p. ej. 'es')")
    return valor


def _validar_timestamp(valor):
    """Acepta ISO-8601 con zona horaria y lo guarda siempre en UTC (AAAA-MM-DDTHH:MM:SSZ), igual que el ETL."""
    if valor is None:
        return valor
    if len(valor) > 40:
        raise ValueError("timestamp demasiado largo")
    try:
        dt = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError("timestamp debe ser ISO-8601 válido, p. ej. 2026-11-23T20:15:00Z") from exc
    if dt.tzinfo is None:
        raise ValueError("timestamp debe indicar la zona horaria (termina en Z para UTC)")
    if dt > datetime.now(timezone.utc) + timedelta(days=1):
        raise ValueError("timestamp no puede estar en el futuro")
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalizar_etiquetas(valor):
    if valor is None:
        return valor
    limpias = []
    for etiqueta in valor:
        etiqueta = str(etiqueta).strip().lower()
        if etiqueta and etiqueta not in limpias:
            limpias.append(etiqueta)
    return limpias


def _validar_texto_segun_tipo(tipo, texto):
    if tipo != "reaccion" and not (texto or "").strip():
        raise ValueError("texto es obligatorio cuando tipo no es 'reaccion'")


def _validar_sentimiento(valor):
    if valor is not None and not -1.0 <= float(valor) <= 1.0:
        raise ValueError("sentimiento debe estar en [-1.0, 1.0]")
    return float(valor) if valor is not None else valor


class InteraccionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_interaccion: str = Field(..., max_length=10)
    id_usuario: str = Field(..., max_length=10)
    timestamp: str
    canal: str
    tipo: str
    texto: str | None = Field(default=None, max_length=MAX_TEXTO)
    tema: str
    sentimiento: float
    idioma: str
    dispositivo: str
    longitud_texto: int | None = None
    reacciones_recibidas: int = Field(..., ge=0)
    etiquetas: list[str] = Field(default_factory=list, max_length=MAX_ETIQUETAS * 5)

    @field_validator("id_interaccion")
    @classmethod
    def validar_id_interaccion(cls, v: str):
        if not ID_INTERACCION_RE.match(v):
            raise ValueError("id_interaccion debe cumplir ^I\\d+$")
        return v

    @field_validator("id_usuario")
    @classmethod
    def validar_id_usuario(cls, v: str):
        if not ID_USUARIO_RE.match(v):
            raise ValueError("id_usuario debe cumplir ^U\\d+$")
        return v

    @field_validator("canal")
    @classmethod
    def validar_canal(cls, v: str):
        return _validar_enum(v, CANALES_VALIDOS, "canal")

    @field_validator("tipo")
    @classmethod
    def validar_tipo(cls, v: str):
        return _validar_enum(v, TIPOS_VALIDOS, "tipo")

    @field_validator("tema")
    @classmethod
    def validar_tema(cls, v: str):
        return _validar_enum(v, TEMAS_VALIDOS, "tema")

    @field_validator("dispositivo")
    @classmethod
    def validar_dispositivo(cls, v: str):
        return _validar_enum(v, DISPOSITIVOS_VALIDOS, "dispositivo")

    @field_validator("idioma")
    @classmethod
    def validar_idioma(cls, v: str):
        return _validar_idioma(v)

    @field_validator("sentimiento")
    @classmethod
    def validar_sentimiento(cls, v: float):
        return _validar_sentimiento(v)

    @field_validator("timestamp")
    @classmethod
    def validar_timestamp(cls, v: str):
        return _validar_timestamp(v)

    @field_validator("etiquetas")
    @classmethod
    def validar_etiquetas(cls, v: list[str]):
        return _normalizar_etiquetas(v)

    @model_validator(mode="after")
    def validar_texto(self):
        _validar_texto_segun_tipo(self.tipo, self.texto)
        return self


class InteraccionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str | None = None
    canal: str | None = None
    tipo: str | None = None
    texto: str | None = Field(default=None, max_length=MAX_TEXTO)
    tema: str | None = None
    sentimiento: float | None = None
    idioma: str | None = None
    dispositivo: str | None = None
    longitud_texto: int | None = None
    reacciones_recibidas: int | None = Field(default=None, ge=0)
    etiquetas: list[str] | None = Field(default=None, max_length=MAX_ETIQUETAS * 5)

    @field_validator("canal")
    @classmethod
    def validar_canal(cls, v: str | None):
        return _validar_enum(v, CANALES_VALIDOS, "canal")

    @field_validator("tipo")
    @classmethod
    def validar_tipo(cls, v: str | None):
        return _validar_enum(v, TIPOS_VALIDOS, "tipo")

    @field_validator("tema")
    @classmethod
    def validar_tema(cls, v: str | None):
        return _validar_enum(v, TEMAS_VALIDOS, "tema")

    @field_validator("dispositivo")
    @classmethod
    def validar_dispositivo(cls, v: str | None):
        return _validar_enum(v, DISPOSITIVOS_VALIDOS, "dispositivo")

    @field_validator("idioma")
    @classmethod
    def validar_idioma(cls, v: str | None):
        return _validar_idioma(v)

    @field_validator("sentimiento")
    @classmethod
    def validar_sentimiento(cls, v: float | None):
        return _validar_sentimiento(v)

    @field_validator("timestamp")
    @classmethod
    def validar_timestamp(cls, v: str | None):
        return _validar_timestamp(v)

    @field_validator("etiquetas")
    @classmethod
    def validar_etiquetas(cls, v: list[str] | None):
        return _normalizar_etiquetas(v)


def _env(nombre: str, default: str | None = None) -> str:
    valor = os.getenv(nombre, default)
    if valor is None or valor == "":
        raise RuntimeError(f"Falta variable de entorno: {nombre}")
    return valor


def ejecutar_d1(sql: str, params: list | None = None) -> dict:
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{_env('CLOUDFLARE_ACCOUNT_ID')}/d1/database/"
        f"{_env('CLOUDFLARE_DATABASE_ID')}/query"
    )
    headers = {
        "Authorization": f"Bearer {_env('CLOUDFLARE_API_TOKEN')}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"sql": sql}
    if params is not None:
        payload["params"] = params
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Cloudflare D1 no disponible: {type(exc).__name__}") from exc
    # El detalle técnico de Cloudflare se registra solo en la consola del servidor; al cliente
    # le llega un mensaje genérico, sin SQL, nombres de tablas ni identificadores internos.
    if resp.status_code >= 400:
        print(f"[D1] HTTP {resp.status_code}: {resp.text[:500]}")
        raise HTTPException(status_code=502, detail="Error al consultar la base de datos")
    data = resp.json()
    if not data.get("success"):
        errores = data.get("errors")
        print(f"[D1] Consulta rechazada: {errores}")
        if "tokens_revocados" in str(errores):
            raise HTTPException(status_code=502, detail="Falta la tabla tokens_revocados")
        raise HTTPException(status_code=502, detail="Error al consultar la base de datos")
    return data["result"][0]


def get_firestore():
    global FIRESTORE_DB
    if FIRESTORE_DB is not None:
        return FIRESTORE_DB
    # En hosting (Render/Railway) la credencial puede venir completa en FIREBASE_CREDENTIALS_JSON;
    # en local se usa el archivo de FIREBASE_CREDENTIALS_PATH.
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        certificado = credentials.Certificate(json.loads(cred_json))
    else:
        cred_path = os.path.join(BASE_DIR, _env("FIREBASE_CREDENTIALS_PATH"))
        if not os.path.isfile(cred_path):
            raise RuntimeError(f"No se encontró credencial Firebase: {cred_path}")
        certificado = credentials.Certificate(cred_path)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(certificado)
    FIRESTORE_DB = firestore.client()
    return FIRESTORE_DB


def crear_jwt(username: str, rol: str) -> str:
    secret = _env("JWT_SECRET_KEY")
    expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "180"))
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "rol": rol,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decodificar_jwt(token: str) -> dict:
    secret = _env("JWT_SECRET_KEY")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    if not payload.get("sub") or not payload.get("jti"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido; inicia sesión de nuevo")
    if payload["jti"] in TOKENS_REVOCADOS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revocado")
    return payload


def validar_sesion(token: str) -> dict:
    """Valida firma, expiración, revocación (memoria + D1) y que el usuario siga activo."""
    payload = decodificar_jwt(token)
    sql = (
        f"SELECT {', '.join(COLUMNAS_PERFIL_USUARIO)}, "
        "EXISTS(SELECT 1 FROM tokens_revocados WHERE jti = ?) AS revocado "
        "FROM usuarios_sistema WHERE username = ? LIMIT 1"
    )
    params = [payload["jti"], payload["sub"]]
    try:
        res = ejecutar_d1(sql, params)
    except HTTPException as exc:
        if "tokens_revocados" not in str(exc.detail):
            raise
        ejecutar_d1(ESQUEMA_TOKENS_REVOCADOS)  # la tabla no existía: se crea y se reintenta una vez
        res = ejecutar_d1(sql, params)
    rows = res.get("results", [])
    if not rows or int(rows[0].get("activo", 0)) != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no autorizado")
    if int(rows[0].get("revocado") or 0):
        TOKENS_REVOCADOS.add(payload["jti"])
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revocado")
    fila = rows[0]
    return {
        "username": fila["username"],
        "nombre_completo": fila.get("nombre_completo"),
        "rol": fila.get("rol"),
        "token": token,
        "jti": payload["jti"],
        "exp": payload.get("exp"),
    }


# Dependencia síncrona: FastAPI la ejecuta en un hilo aparte, así la consulta a D1
# no congela el servidor (ni los WebSockets) mientras espera la respuesta.
def obtener_usuario_actual(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta token Bearer")
    return validar_sesion(credentials.credentials)


def _normalizar_interaccion_doc(doc: dict) -> dict:
    data = dict(doc)
    data["etiquetas"] = data.get("etiquetas") or []
    for clave, valor in list(data.items()):
        if isinstance(valor, float) and not math.isfinite(valor):
            data[clave] = None
    return data


def _calcular_longitud_texto(tipo: str, texto: str | None) -> int:
    if tipo == "reaccion":
        return 0
    return len(texto or "")


def validar_id_interaccion_ruta(id_interaccion: str) -> str:
    if not ID_INTERACCION_RE.match(id_interaccion or ""):
        raise HTTPException(status_code=400, detail="id_interaccion debe tener el formato I seguido de números (p. ej. I0001)")
    return id_interaccion


def validar_usuario_en_d1(id_usuario: str) -> None:
    res = ejecutar_d1("SELECT id_usuario FROM usuarios WHERE id_usuario = ? LIMIT 1", [id_usuario])
    if not res.get("results"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="id_usuario no existe en Cloudflare D1 (usuarios)")


def metricas_vacias() -> dict:
    return calcular_metricas([])


def recalcular_metricas_dinamicas(guardar_en_firestore: bool = True) -> dict:
    db = get_firestore()
    interacciones = [_normalizar_interaccion_doc(d.to_dict() or {}) for d in db.collection("interacciones").stream(**FS)]
    metricas = calcular_metricas(interacciones)
    if guardar_en_firestore:
        db.collection("metricas_tiempo_real").document("resumen_global").set(metricas, **FS)
    return metricas


async def emitir_evento_interaccion(evento: str, interaccion: dict | None, actor: str):
    metricas = await run_in_threadpool(recalcular_metricas_dinamicas, True)
    payload = {
        "type": evento,
        "evento": evento,
        "actor": actor,
        "timestamp_utc": utc_now_iso(),
        "interaccion": interaccion,
        "metricas": metricas,
    }
    await gestor_ws.broadcast(payload)


def _quitar_campos_geo(data: dict) -> None:
    for k in ["pais", "estado", "ciudad", "latitud", "longitud", "zona_horaria"]:
        data.pop(k, None)


def _crear_interaccion_sync(payload: InteraccionBase, usuario: dict) -> dict:
    db = get_firestore()
    doc_ref = db.collection("interacciones").document(payload.id_interaccion)
    if doc_ref.get(**FS).exists:
        raise HTTPException(status_code=409, detail="id_interaccion ya existe en Firestore")

    validar_usuario_en_d1(payload.id_usuario)

    data = payload.model_dump()
    if data["tipo"] == "reaccion":
        data["texto"] = None
    data["longitud_texto"] = _calcular_longitud_texto(data["tipo"], data.get("texto"))
    data["auditoria"] = {
        "origen": "api_rest_postman",
        "fecha_carga_utc": utc_now_iso(),
        "regla_privacidad": "sin_datos_geo",
        "actor": usuario["username"],
    }
    _quitar_campos_geo(data)
    doc_ref.set(data, **FS)
    return data


def _actualizar_interaccion_sync(id_interaccion: str, payload: InteraccionUpdate, usuario: dict) -> dict:
    cambios = payload.model_dump(exclude_unset=True)
    if not cambios:
        raise HTTPException(status_code=400, detail="No se proporcionaron campos para actualizar")
    obligatorios = ["timestamp", "canal", "tipo", "tema", "sentimiento", "idioma", "dispositivo", "reacciones_recibidas"]
    nulos = [c for c in obligatorios if c in cambios and cambios[c] is None]
    if nulos:
        raise HTTPException(status_code=400, detail=f"Estos campos no pueden ser null: {', '.join(nulos)}")
    if "etiquetas" in cambios and cambios["etiquetas"] is None:
        cambios["etiquetas"] = []

    db = get_firestore()
    doc_ref = db.collection("interacciones").document(id_interaccion)
    doc = doc_ref.get(**FS)
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")

    actual = _normalizar_interaccion_doc(doc.to_dict() or {})
    validar_usuario_en_d1(str(actual.get("id_usuario") or ""))
    actualizado = {**actual, **cambios}
    actualizado["id_interaccion"] = actual.get("id_interaccion", id_interaccion)
    actualizado["id_usuario"] = actual.get("id_usuario")

    if actualizado["tipo"] == "reaccion":
        actualizado["texto"] = None
    try:
        _validar_texto_segun_tipo(actualizado["tipo"], actualizado.get("texto"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    actualizado["longitud_texto"] = _calcular_longitud_texto(actualizado["tipo"], actualizado.get("texto"))

    auditoria_prev = actual.get("auditoria") or {}
    actualizado["auditoria"] = {
        **auditoria_prev,
        "origen": auditoria_prev.get("origen", "api_rest_postman"),
        "fecha_carga_utc": auditoria_prev.get("fecha_carga_utc", utc_now_iso()),
        "fecha_actualizacion_utc": utc_now_iso(),
        "regla_privacidad": "sin_datos_geo",
        "actor": usuario["username"],
    }
    _quitar_campos_geo(actualizado)
    doc_ref.set(actualizado, **FS)
    return actualizado


def _eliminar_interaccion_sync(id_interaccion: str) -> dict:
    db = get_firestore()
    doc_ref = db.collection("interacciones").document(id_interaccion)
    doc = doc_ref.get(**FS)
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")
    data = _normalizar_interaccion_doc(doc.to_dict() or {})
    doc_ref.delete(**FS)
    return {"id_interaccion": id_interaccion, **data}


@app.get("/")
async def root():
    return RedirectResponse(url="/static/login.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard(request: Request):
    """El HTML del dashboard solo se entrega con una sesión válida (cookie HttpOnly emitida en el login)."""
    token = request.cookies.get(COOKIE_SESION)
    if token:
        try:
            await run_in_threadpool(validar_sesion, token)
            return FileResponse(
                RUTA_DASHBOARD_HTML,
                media_type="text/html",
                headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
            )
        except HTTPException:
            pass
    redireccion = RedirectResponse(url="/static/login.html?sesion=expirada", status_code=status.HTTP_303_SEE_OTHER)
    redireccion.delete_cookie(COOKIE_SESION, path="/")
    return redireccion


@app.get("/static/dashboard.html", include_in_schema=False)
async def dashboard_ruta_antigua():
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_301_MOVED_PERMANENTLY)


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    clave = _clave_intento(request, req.username)
    _verificar_bloqueo_login(clave)
    # Se puede entrar con el username o con el correo (el correo se guarda en minúsculas).
    identificador = req.username.strip()
    clave_ingresada = req.password.strip()
    res = ejecutar_d1(
        f"SELECT {', '.join(COLUMNAS_LOGIN_USUARIO)} FROM usuarios_sistema "
        "WHERE username = ? OR (correo IS NOT NULL AND correo = ?) LIMIT 1",
        [identificador, identificador.lower()],
    )
    rows = res.get("results", [])
    user = rows[0] if rows else None
    password_hash = ((user or {}).get("password_hash") or "").encode("utf-8")
    try:
        valida = bool(user) and bcrypt.checkpw(clave_ingresada.encode("utf-8"), password_hash)
    except ValueError:
        valida = False
    # Mismo mensaje para usuario inexistente, contraseña incorrecta o usuario inactivo:
    # así el login no revela qué usuarios existen.
    if not valida or int(user.get("activo", 0)) != 1:
        _registrar_fallo_login(clave)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    with _intentos_lock:
        _intentos_login.pop(clave, None)
    token = crear_jwt(user["username"], user.get("rol") or "administrador")
    response.set_cookie(
        COOKIE_SESION,
        token,
        max_age=int(os.getenv("JWT_EXPIRE_MINUTES", "180")) * 60,
        httponly=True,  # JavaScript (y F12 → Console) no puede leerla
        samesite="strict",
        secure=COOKIE_SECURE,
        path="/",
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "usuario": {
            "username": user["username"],
            "nombre_completo": user.get("nombre_completo"),
            "rol": user.get("rol"),
        },
    }


@app.get("/api/auth/verify")
async def verify(usuario=Depends(obtener_usuario_actual)):
    return {"ok": True, "usuario": {"username": usuario["username"], "nombre_completo": usuario["nombre_completo"], "rol": usuario["rol"]}}


def _revocar_en_d1(payload: dict) -> bool:
    try:
        ejecutar_d1(ESQUEMA_TOKENS_REVOCADOS)
        ejecutar_d1(
            "INSERT OR IGNORE INTO tokens_revocados (jti, username, expira_epoch, revocado_utc) VALUES (?, ?, ?, ?)",
            [payload["jti"], payload["sub"], int(payload.get("exp") or 0), utc_now_iso()],
        )
        ejecutar_d1("DELETE FROM tokens_revocados WHERE expira_epoch < ?", [int(datetime.now(timezone.utc).timestamp())])
        return True
    except Exception as exc:
        print(f"[AVISO] No se pudo registrar la revocación en D1: {exc}")
        return False


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    # Cerrar sesión solo necesita un token bien firmado: no depende de que el usuario siga activo.
    response.delete_cookie(COOKIE_SESION, path="/")
    token = credentials.credentials if credentials and credentials.credentials else request.cookies.get(COOKIE_SESION)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta token Bearer")
    try:
        payload = decodificar_jwt(token)
    except HTTPException as exc:
        if exc.detail == "Token revocado":
            return {"ok": True, "mensaje": "La sesión ya estaba cerrada"}
        raise
    TOKENS_REVOCADOS.add(payload["jti"])
    persistida = _revocar_en_d1(payload)
    return {"ok": True, "mensaje": "Sesión cerrada", "revocacion_persistida_en_d1": persistida}


@app.get("/api/health")
def health():
    estado = {"api": "ok", "d1": "ok", "firestore": "ok"}
    try:
        ejecutar_d1("SELECT 1 AS ok")
    except Exception as exc:
        print(f"[health] D1: {getattr(exc, 'detail', exc)}")
        estado["d1"] = "error"
    try:
        get_firestore().collection("metricas_tiempo_real").document("resumen_global").get(
            retry=Retry(initial=0.5, maximum=1.0, multiplier=2.0, timeout=5), timeout=5
        )
    except Exception as exc:
        print(f"[health] Firestore: {type(exc).__name__}: {exc}")
        estado["firestore"] = "error"
    codigo = 200 if estado["d1"] == "ok" and estado["firestore"] == "ok" else 503
    return JSONResponse(status_code=codigo, content=estado)


@app.get("/api/interacciones")
def listar_interacciones(
    canal: str | None = None,
    tema: str | None = None,
    tipo: str | None = None,
    id_usuario: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    usuario=Depends(obtener_usuario_actual),
):
    db = get_firestore()
    docs = [_normalizar_interaccion_doc(d.to_dict() or {}) for d in db.collection("interacciones").stream(**FS)]
    filtros = {"canal": canal, "tema": tema, "tipo": tipo, "id_usuario": id_usuario}
    for campo, valor in filtros.items():
        if valor:
            docs = [d for d in docs if d.get(campo) == valor]
    docs.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return {"total": len(docs), "limit": limit, "offset": offset, "interacciones": docs[offset : offset + limit]}


@app.get("/api/interacciones/{id_interaccion}")
def obtener_interaccion(id_interaccion: str, usuario=Depends(obtener_usuario_actual)):
    validar_id_interaccion_ruta(id_interaccion)
    db = get_firestore()
    doc = db.collection("interacciones").document(id_interaccion).get(**FS)
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")
    return _normalizar_interaccion_doc(doc.to_dict() or {})


@app.post("/api/interacciones", status_code=status.HTTP_201_CREATED)
async def crear_interaccion(payload: InteraccionBase, usuario=Depends(obtener_usuario_actual)):
    data = await run_in_threadpool(_crear_interaccion_sync, payload, usuario)
    await emitir_evento_interaccion("interaccion_creada", data, usuario["username"])
    return data


@app.put("/api/interacciones/{id_interaccion}")
async def actualizar_interaccion(id_interaccion: str, payload: InteraccionUpdate, usuario=Depends(obtener_usuario_actual)):
    validar_id_interaccion_ruta(id_interaccion)
    actualizado = await run_in_threadpool(_actualizar_interaccion_sync, id_interaccion, payload, usuario)
    await emitir_evento_interaccion("interaccion_actualizada", actualizado, usuario["username"])
    return actualizado


@app.delete("/api/interacciones/{id_interaccion}")
async def eliminar_interaccion(id_interaccion: str, usuario=Depends(obtener_usuario_actual)):
    validar_id_interaccion_ruta(id_interaccion)
    eliminada = await run_in_threadpool(_eliminar_interaccion_sync, id_interaccion)
    await emitir_evento_interaccion("interaccion_eliminada", eliminada, usuario["username"])
    return {"ok": True, "mensaje": "Interacción eliminada", "id_interaccion": id_interaccion}


@app.get("/api/historico/dashboard")
def historico_dashboard(usuario=Depends(obtener_usuario_actual)):
    consultas = {
        "total_usuarios": "SELECT COUNT(*) AS total FROM usuarios",
        "con_consent": "SELECT COUNT(*) AS total FROM usuarios WHERE consentimiento_datos = 1",
        "sin_consent": "SELECT COUNT(*) AS total FROM usuarios WHERE consentimiento_datos = 0",
        "total_artistas": "SELECT COUNT(*) AS total FROM usuarios WHERE es_artista = 1",
        "total_cuarentena": "SELECT COUNT(*) AS total FROM interacciones_cuarentena",
        "por_plataforma": "SELECT plataforma_principal, COUNT(*) AS total FROM usuarios GROUP BY plataforma_principal ORDER BY total DESC",
        "por_interes": "SELECT interes_principal, COUNT(*) AS total FROM usuarios GROUP BY interes_principal ORDER BY total DESC",
        "por_rango_edad": "SELECT rango_edad, COUNT(*) AS total FROM usuarios GROUP BY rango_edad ORDER BY total DESC",
        "top_ciudades": """
            SELECT uu.ciudad, uu.estado, uu.pais, COUNT(*) AS total
            FROM usuarios_ubicacion uu
            INNER JOIN usuarios u ON u.id_usuario = uu.id_usuario
            WHERE u.consentimiento_datos = 1
            GROUP BY uu.ciudad, uu.estado, uu.pais
            ORDER BY total DESC
            LIMIT 10
        """,
        "riesgo": """
            SELECT
                CASE
                    WHEN (mensajes_30d + reacciones_30d) <= 5 THEN 'Riesgo alto'
                    WHEN (mensajes_30d + reacciones_30d) <= 20 THEN 'Riesgo medio'
                    ELSE 'Activos'
                END AS categoria,
                COUNT(*) AS total
            FROM usuarios_metricas
            GROUP BY categoria
        """,
    }
    # Las 10 consultas a D1 son independientes: se lanzan en paralelo para que el dashboard cargue rápido.
    with ThreadPoolExecutor(max_workers=len(consultas)) as pool:
        futuros = {clave: pool.submit(ejecutar_d1, sql) for clave, sql in consultas.items()}
        r = {clave: futuro.result().get("results", []) for clave, futuro in futuros.items()}

    total_usuarios = r["total_usuarios"][0]["total"]
    con_consent = r["con_consent"][0]["total"]
    sin_consent = r["sin_consent"][0]["total"]
    total_artistas = r["total_artistas"][0]["total"]
    total_cuarentena = r["total_cuarentena"][0]["total"]
    por_plataforma = r["por_plataforma"]
    por_interes = r["por_interes"]
    por_rango_edad = r["por_rango_edad"]
    top_ciudades = r["top_ciudades"]
    riesgo_rows = r["riesgo"]
    riesgo = {"Riesgo alto": 0, "Riesgo medio": 0, "Activos": 0}
    for r in riesgo_rows:
        riesgo[r["categoria"]] = r["total"]

    error_firestore = None
    try:
        metricas_firestore = recalcular_metricas_dinamicas(guardar_en_firestore=False)
    except Exception as exc:
        print(f"[historico] No se pudieron leer las métricas de Firestore: {exc}")
        error_firestore = "No se pudieron cargar los datos en vivo"
        metricas_firestore = metricas_vacias()

    return {
        "resumen_usuarios": {
            "total_usuarios": total_usuarios,
            "con_consentimiento_geo": con_consent,
            "sin_consentimiento_geo": sin_consent,
            "total_artistas": total_artistas,
        },
        "interacciones_cuarentena": total_cuarentena,
        "por_plataforma_principal": por_plataforma,
        "por_interes_principal": por_interes,
        "por_rango_edad": por_rango_edad,
        "top_ciudades_autorizadas": top_ciudades,
        "riesgo_abandono": riesgo,
        "total_interacciones": metricas_firestore["total_interacciones"],
        "metricas_firestore": metricas_firestore,
        "metricas_tiempo_real": metricas_firestore,
        "error_firestore": error_firestore,
        "privacidad": {
            "regla": "sin_datos_geo",
            "top_ciudades": "solo usuarios con consentimiento_datos = 1",
        },
    }


SELECT_USUARIO_HISTORICO = """
    SELECT u.id_usuario, u.alias, u.fecha_ingreso, u.rango_edad, u.especie_fursona,
           u.interes_principal, u.plataforma_principal, u.es_artista, u.consentimiento_datos,
           m.mensajes_30d, m.reacciones_30d, m.eventos_asistidos, m.comisiones_compradas,
           m.sentimiento_promedio, m.ultima_actividad, m.estatus,
           uu.pais, uu.estado, uu.ciudad
    FROM usuarios u
    LEFT JOIN usuarios_metricas m ON m.id_usuario = u.id_usuario
    LEFT JOIN usuarios_ubicacion uu ON uu.id_usuario = u.id_usuario AND u.consentimiento_datos = 1
"""


@app.get("/api/historico/usuarios")
def historico_usuarios(
    plataforma: str | None = None,
    interes: str | None = None,
    estatus: str | None = None,
    es_artista: int | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    usuario=Depends(obtener_usuario_actual),
):
    """Padrón consolidado desde D1. La ubicación solo aparece para usuarios con consentimiento."""
    condiciones, params = [], []
    for columna, valor in (
        ("u.plataforma_principal", plataforma),
        ("u.interes_principal", interes),
        ("m.estatus", estatus),
        ("u.es_artista", es_artista),
    ):
        if valor is not None and valor != "":
            condiciones.append(f"{columna} = ?")
            params.append(valor)
    where = f" WHERE {' AND '.join(condiciones)}" if condiciones else ""
    total = ejecutar_d1(
        f"SELECT COUNT(*) AS total FROM usuarios u LEFT JOIN usuarios_metricas m ON m.id_usuario = u.id_usuario{where}",
        params,
    )["results"][0]["total"]
    filas = ejecutar_d1(
        f"{SELECT_USUARIO_HISTORICO}{where} ORDER BY u.id_usuario LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).get("results", [])
    return {"total": total, "limit": limit, "offset": offset, "usuarios": filas}


@app.get("/api/historico/usuarios/{id_usuario}")
def historico_usuario(id_usuario: str, usuario=Depends(obtener_usuario_actual)):
    if not ID_USUARIO_RE.match(id_usuario):
        raise HTTPException(status_code=400, detail="id_usuario debe cumplir ^U\\d+$")
    filas = ejecutar_d1(f"{SELECT_USUARIO_HISTORICO} WHERE u.id_usuario = ? LIMIT 1", [id_usuario]).get("results", [])
    if not filas:
        raise HTTPException(status_code=404, detail="Usuario no encontrado en D1")
    return filas[0]


@app.get("/api/historico/cuarentena")
def historico_cuarentena(
    motivo: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    usuario=Depends(obtener_usuario_actual),
):
    """Interacciones rechazadas por el ETL (no se eliminan: quedan auditables)."""
    where, params = ("", [])
    if motivo:
        where, params = (" WHERE motivo_rechazo = ?", [motivo])
    total = ejecutar_d1(f"SELECT COUNT(*) AS total FROM interacciones_cuarentena{where}", params)["results"][0]["total"]
    por_motivo = ejecutar_d1(
        "SELECT motivo_rechazo, COUNT(*) AS total FROM interacciones_cuarentena GROUP BY motivo_rechazo ORDER BY total DESC"
    ).get("results", [])
    filas = ejecutar_d1(
        "SELECT id_interaccion, id_usuario_origen, timestamp_origen, canal, tipo, tema, motivo_rechazo "
        f"FROM interacciones_cuarentena{where} ORDER BY timestamp_origen DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).get("results", [])
    return {"total": total, "limit": limit, "offset": offset, "por_motivo": por_motivo, "registros": filas}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Se acepta primero para que el navegador reciba el código de cierre real
    # (si se cierra antes de aceptar, el navegador solo ve 1006 y no sabe por qué).
    await websocket.accept()
    # El token llega en el PRIMER mensaje ({"type":"auth","token":"..."}) y no en la URL:
    # así no queda escrito en los registros de uvicorn ni en el historial del navegador.
    try:
        primero = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_S))
        token = primero.get("token") if isinstance(primero, dict) and primero.get("type") == "auth" else None
    except Exception:
        token = None
    if not token or not isinstance(token, str):
        await websocket.close(code=1008)
        return
    try:
        sesion = await run_in_threadpool(validar_sesion, token)
    except HTTPException as exc:
        # 1008 = token inválido o revocado (el frontend manda al login); 1013 = servicio no disponible, reintentar
        await websocket.close(code=1008 if exc.status_code == 401 else 1013)
        return
    except Exception:
        await websocket.close(code=1013)
        return

    await gestor_ws.conectar(websocket, {"jti": sesion["jti"], "exp": sesion.get("exp")})
    try:
        error_metricas = None
        try:
            metricas = await run_in_threadpool(recalcular_metricas_dinamicas, False)
        except Exception as exc:
            # El detalle técnico se queda en la consola; al navegador solo le llega un aviso genérico.
            print(f"[WS] No se pudieron calcular las métricas iniciales: {exc}")
            error_metricas = "No se pudieron cargar los datos en vivo"
            metricas = metricas_vacias()
        await websocket.send_text(json.dumps(sanitizar_json({
            "type": "conexion_inicial",
            "evento": "conexion_inicial",
            "timestamp_utc": utc_now_iso(),
            "metricas": metricas,
            "error": error_metricas,
        }), ensure_ascii=False, default=str, allow_nan=False))
        while True:
            msg = await websocket.receive_text()
            # Si la sesión expiró o se cerró (logout), se corta en el siguiente mensaje (el ping llega cada 25 s).
            if not gestor_ws.sesion_vigente(websocket):
                await gestor_ws.desconectar(websocket)
                await websocket.close(code=1008)
                return
            if len(msg) > 4096:
                continue
            try:
                data = json.loads(msg)
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "timestamp_utc": utc_now_iso()}))
            else:
                await websocket.send_text(json.dumps({"type": "ack", "timestamp_utc": utc_now_iso()}))
    except WebSocketDisconnect:
        await gestor_ws.desconectar(websocket)
    except Exception:
        await gestor_ws.desconectar(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    # Render/Railway asignan el puerto en PORT. proxy_headers hace que uvicorn confíe en
    # X-Forwarded-For/Proto del proxy HTTPS (IP real para el límite de intentos y wss:// correcto).
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
