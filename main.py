import asyncio
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import firebase_admin
import jwt
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from firebase_admin import credentials, firestore
from pydantic import BaseModel, ConfigDict, Field, field_validator

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

ID_INTERACCION_RE = re.compile(r"^I\d+$")
ID_USUARIO_RE = re.compile(r"^U\d+$")
IDIOMA_RE = re.compile(r"^[a-z]{2}$")
COLUMNAS_LOGIN_USUARIO = ("username", "nombre_completo", "password_hash", "rol", "activo")
COLUMNAS_PERFIL_USUARIO = ("username", "nombre_completo", "rol", "activo")

app = FastAPI(title="Community360 API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)
TOKENS_REVOCADOS: set[str] = set()
FIRESTORE_DB = None


class GestorWebSockets:
    def __init__(self):
        self.activos: list[WebSocket] = []
        self.lock = asyncio.Lock()

    async def conectar(self, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            self.activos.append(websocket)

    async def desconectar(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.activos:
                self.activos.remove(websocket)

    async def broadcast(self, mensaje: dict):
        data = json.dumps(sanitizar_json(mensaje), ensure_ascii=False, default=str, allow_nan=False)
        async with self.lock:
            activos = list(self.activos)
        stale = []
        for ws in activos:
            try:
                await ws.send_text(data)
            except Exception:
                stale.append(ws)
        for ws in stale:
            await self.desconectar(ws)


gestor_ws = GestorWebSockets()


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


def _validar_enum(valor, permitidos, campo):
    if valor is not None and valor not in permitidos:
        raise ValueError(f"{campo} inválido. Permitidos: {permitidos}")
    return valor


def _validar_idioma(valor):
    if valor is not None and not IDIOMA_RE.match(valor):
        raise ValueError("idioma debe ser un código ISO-639-1 de dos letras minúsculas (p. ej. 'es')")
    return valor


def _validar_timestamp(valor):
    if valor is None:
        return valor
    try:
        datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError("timestamp debe ser ISO-8601 válido") from exc
    return valor


def _validar_sentimiento(valor):
    if valor is not None and not -1.0 <= float(valor) <= 1.0:
        raise ValueError("sentimiento debe estar en [-1.0, 1.0]")
    return float(valor) if valor is not None else valor


class InteraccionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_interaccion: str
    id_usuario: str
    timestamp: str
    canal: str
    tipo: str
    texto: str | None = None
    tema: str
    sentimiento: float
    idioma: str
    dispositivo: str
    longitud_texto: int | None = None
    reacciones_recibidas: int = Field(..., ge=0)
    etiquetas: list[str] = Field(default_factory=list)

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


class InteraccionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str | None = None
    canal: str | None = None
    tipo: str | None = None
    texto: str | None = None
    tema: str | None = None
    sentimiento: float | None = None
    idioma: str | None = None
    dispositivo: str | None = None
    longitud_texto: int | None = None
    reacciones_recibidas: int | None = Field(default=None, ge=0)
    etiquetas: list[str] | None = None

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
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Error D1 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not data.get("success"):
        raise HTTPException(status_code=502, detail=f"D1 rechazó consulta: {data.get('errors')}")
    return data["result"][0]


def get_firestore():
    global FIRESTORE_DB
    if FIRESTORE_DB is not None:
        return FIRESTORE_DB
    cred_path = os.path.join(BASE_DIR, _env("FIREBASE_CREDENTIALS_PATH"))
    if not os.path.isfile(cred_path):
        raise RuntimeError(f"No se encontró credencial Firebase: {cred_path}")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_path))
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
    if token in TOKENS_REVOCADOS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revocado")
    return payload


# Dependencia síncrona: FastAPI la ejecuta en un hilo aparte, así la consulta a D1
# no congela el servidor (ni los WebSockets) mientras espera la respuesta.
def obtener_usuario_actual(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta token Bearer")
    token = credentials.credentials
    payload = decodificar_jwt(token)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    res = ejecutar_d1(
        f"SELECT {', '.join(COLUMNAS_PERFIL_USUARIO)} FROM usuarios_sistema WHERE username = ? LIMIT 1",
        [username],
    )
    rows = res.get("results", [])
    if not rows or int(rows[0].get("activo", 0)) != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario no autorizado")
    return {"username": rows[0]["username"], "nombre_completo": rows[0].get("nombre_completo"), "rol": rows[0].get("rol"), "token": token}


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


def validar_usuario_en_d1(id_usuario: str) -> None:
    res = ejecutar_d1("SELECT id_usuario FROM usuarios WHERE id_usuario = ? LIMIT 1", [id_usuario])
    if not res.get("results"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="id_usuario no existe en Cloudflare D1 (usuarios)")


def metricas_vacias() -> dict:
    return calcular_metricas([])


def recalcular_metricas_dinamicas(guardar_en_firestore: bool = True) -> dict:
    db = get_firestore()
    interacciones = [_normalizar_interaccion_doc(d.to_dict() or {}) for d in db.collection("interacciones").stream()]
    metricas = calcular_metricas(interacciones)
    if guardar_en_firestore:
        db.collection("metricas_tiempo_real").document("resumen_global").set(metricas)
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
    if doc_ref.get().exists:
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
    doc_ref.set(data)
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
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")

    actual = _normalizar_interaccion_doc(doc.to_dict() or {})
    validar_usuario_en_d1(str(actual.get("id_usuario") or ""))
    actualizado = {**actual, **cambios}
    actualizado["id_interaccion"] = actual.get("id_interaccion", id_interaccion)
    actualizado["id_usuario"] = actual.get("id_usuario")

    if actualizado["tipo"] == "reaccion":
        actualizado["texto"] = None
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
    doc_ref.set(actualizado)
    return actualizado


def _eliminar_interaccion_sync(id_interaccion: str) -> dict:
    db = get_firestore()
    doc_ref = db.collection("interacciones").document(id_interaccion)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")
    data = _normalizar_interaccion_doc(doc.to_dict() or {})
    doc_ref.delete()
    return {"id_interaccion": id_interaccion, **data}


@app.get("/")
async def root():
    return RedirectResponse(url="/static/login.html")


@app.post("/api/auth/login")
def login(req: LoginRequest):
    res = ejecutar_d1(
        f"SELECT {', '.join(COLUMNAS_LOGIN_USUARIO)} FROM usuarios_sistema WHERE username = ? LIMIT 1",
        [req.username.strip()],
    )
    rows = res.get("results", [])
    if not rows:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    user = rows[0]
    if int(user.get("activo", 0)) != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario inactivo")
    password_hash = (user.get("password_hash") or "").encode("utf-8")
    try:
        valida = bcrypt.checkpw(req.password.encode("utf-8"), password_hash)
    except ValueError:
        valida = False
    if not valida:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    token = crear_jwt(user["username"], user.get("rol") or "administrador")
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


@app.post("/api/auth/logout")
async def logout(usuario=Depends(obtener_usuario_actual)):
    TOKENS_REVOCADOS.add(usuario["token"])
    return {"ok": True, "mensaje": "Sesión cerrada"}


@app.get("/api/interacciones")
def listar_interacciones(canal: str | None = None, tema: str | None = None, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    docs = [_normalizar_interaccion_doc(d.to_dict() or {}) for d in db.collection("interacciones").stream()]
    if canal:
        docs = [d for d in docs if d.get("canal") == canal]
    if tema:
        docs = [d for d in docs if d.get("tema") == tema]
    docs.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return {"total": len(docs), "interacciones": docs}


@app.get("/api/interacciones/{id_interaccion}")
def obtener_interaccion(id_interaccion: str, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    doc = db.collection("interacciones").document(id_interaccion).get()
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
    actualizado = await run_in_threadpool(_actualizar_interaccion_sync, id_interaccion, payload, usuario)
    await emitir_evento_interaccion("interaccion_actualizada", actualizado, usuario["username"])
    return actualizado


@app.delete("/api/interacciones/{id_interaccion}")
async def eliminar_interaccion(id_interaccion: str, usuario=Depends(obtener_usuario_actual)):
    eliminada = await run_in_threadpool(_eliminar_interaccion_sync, id_interaccion)
    await emitir_evento_interaccion("interaccion_eliminada", eliminada, usuario["username"])
    return {"ok": True, "mensaje": "Interacción eliminada", "id_interaccion": id_interaccion}


@app.get("/api/historico/dashboard")
def historico_dashboard(usuario=Depends(obtener_usuario_actual)):
    total_usuarios = ejecutar_d1("SELECT COUNT(*) AS total FROM usuarios")["results"][0]["total"]
    con_consent = ejecutar_d1("SELECT COUNT(*) AS total FROM usuarios WHERE consentimiento_datos = 1")["results"][0]["total"]
    sin_consent = ejecutar_d1("SELECT COUNT(*) AS total FROM usuarios WHERE consentimiento_datos = 0")["results"][0]["total"]
    total_artistas = ejecutar_d1("SELECT COUNT(*) AS total FROM usuarios WHERE es_artista = 1")["results"][0]["total"]
    total_cuarentena = ejecutar_d1("SELECT COUNT(*) AS total FROM interacciones_cuarentena")["results"][0]["total"]

    por_plataforma = ejecutar_d1(
        "SELECT plataforma_principal, COUNT(*) AS total FROM usuarios GROUP BY plataforma_principal ORDER BY total DESC"
    ).get("results", [])
    por_interes = ejecutar_d1(
        "SELECT interes_principal, COUNT(*) AS total FROM usuarios GROUP BY interes_principal ORDER BY total DESC"
    ).get("results", [])
    por_rango_edad = ejecutar_d1(
        "SELECT rango_edad, COUNT(*) AS total FROM usuarios GROUP BY rango_edad ORDER BY total DESC"
    ).get("results", [])

    top_ciudades = ejecutar_d1(
        """
        SELECT uu.ciudad, uu.estado, uu.pais, COUNT(*) AS total
        FROM usuarios_ubicacion uu
        INNER JOIN usuarios u ON u.id_usuario = uu.id_usuario
        WHERE u.consentimiento_datos = 1
        GROUP BY uu.ciudad, uu.estado, uu.pais
        ORDER BY total DESC
        LIMIT 10
        """
    ).get("results", [])

    riesgo_rows = ejecutar_d1(
        """
        SELECT
            CASE
                WHEN (mensajes_30d + reacciones_30d) <= 5 THEN 'Riesgo alto'
                WHEN (mensajes_30d + reacciones_30d) <= 20 THEN 'Riesgo medio'
                ELSE 'Activos'
            END AS categoria,
            COUNT(*) AS total
        FROM usuarios_metricas
        GROUP BY categoria
        """
    ).get("results", [])
    riesgo = {"Riesgo alto": 0, "Riesgo medio": 0, "Activos": 0}
    for r in riesgo_rows:
        riesgo[r["categoria"]] = r["total"]

    error_firestore = None
    try:
        metricas_firestore = recalcular_metricas_dinamicas(guardar_en_firestore=False)
    except Exception as exc:
        error_firestore = f"No se pudieron leer las métricas de Firestore: {exc}"
        print(error_firestore)
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
            "filtro_top_ciudades": "INNER JOIN usuarios_ubicacion uu ON u.id_usuario = uu.id_usuario WHERE u.consentimiento_datos = 1",
        },
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = Query(default=None)):
    if not token:
        await websocket.close(code=1008)
        return
    try:
        decodificar_jwt(token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    except Exception:
        await websocket.close(code=1008)
        return

    await gestor_ws.conectar(websocket)
    try:
        error_metricas = None
        try:
            metricas = await run_in_threadpool(recalcular_metricas_dinamicas, False)
        except Exception as exc:
            error_metricas = f"No se pudieron calcular las métricas iniciales: {exc}"
            print(error_metricas)
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
            try:
                data = json.loads(msg)
            except Exception:
                data = {"type": msg}
            if data.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "timestamp_utc": utc_now_iso()}))
            else:
                await websocket.send_text(json.dumps({"type": "ack", "recibido": data.get("type"), "timestamp_utc": utc_now_iso()}))
    except WebSocketDisconnect:
        await gestor_ws.desconectar(websocket)
    except Exception:
        await gestor_ws.desconectar(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
