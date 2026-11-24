import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import firebase_admin
import jwt
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from firebase_admin import credentials, firestore
from pydantic import BaseModel, ConfigDict, Field, field_validator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CANALES_VALIDOS = ["Telegram", "Facebook", "Instagram", "TikTok", "Twitter/X", "Discord"]
TIPOS_VALIDOS = ["mensaje", "comentario", "encuesta", "reaccion"]
DIAS_HEATMAP = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
BLOQUES_HEATMAP = ["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"]
ID_INTERACCION_RE = re.compile(r"^I\d+$")
ID_USUARIO_RE = re.compile(r"^U\d+$")
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
        data = json.dumps(mensaje, ensure_ascii=False, default=str)
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
        if v not in CANALES_VALIDOS:
            raise ValueError(f"canal inválido. Permitidos: {CANALES_VALIDOS}")
        return v

    @field_validator("tipo")
    @classmethod
    def validar_tipo(cls, v: str):
        if v not in TIPOS_VALIDOS:
            raise ValueError(f"tipo inválido. Permitidos: {TIPOS_VALIDOS}")
        return v

    @field_validator("sentimiento")
    @classmethod
    def validar_sentimiento(cls, v: float):
        if not -1.0 <= float(v) <= 1.0:
            raise ValueError("sentimiento debe estar en [-1.0, 1.0]")
        return float(v)

    @field_validator("timestamp")
    @classmethod
    def validar_timestamp(cls, v: str):
        try:
            if v.endswith("Z"):
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            else:
                datetime.fromisoformat(v)
        except Exception as exc:
            raise ValueError("timestamp debe ser ISO-8601 válido") from exc
        return v


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
        if v is not None and v not in CANALES_VALIDOS:
            raise ValueError(f"canal inválido. Permitidos: {CANALES_VALIDOS}")
        return v

    @field_validator("tipo")
    @classmethod
    def validar_tipo(cls, v: str | None):
        if v is not None and v not in TIPOS_VALIDOS:
            raise ValueError(f"tipo inválido. Permitidos: {TIPOS_VALIDOS}")
        return v

    @field_validator("sentimiento")
    @classmethod
    def validar_sentimiento(cls, v: float | None):
        if v is not None and not -1.0 <= float(v) <= 1.0:
            raise ValueError("sentimiento debe estar en [-1.0, 1.0]")
        return float(v) if v is not None else v

    @field_validator("timestamp")
    @classmethod
    def validar_timestamp(cls, v: str | None):
        if v is None:
            return v
        try:
            if v.endswith("Z"):
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            else:
                datetime.fromisoformat(v)
        except Exception as exc:
            raise ValueError("timestamp debe ser ISO-8601 válido") from exc
        return v


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def crear_jwt(username: str, rol: str) -> str:
    secret = _env("JWT_SECRET_KEY")
    expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))
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


async def obtener_usuario_actual(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
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
    return data


def _calcular_longitud_texto(tipo: str, texto: str | None) -> int:
    if tipo == "reaccion":
        return 0
    return len(texto or "")


def recalcular_metricas_dinamicas(guardar_en_firestore: bool = True) -> dict:
    db = get_firestore()
    docs = list(db.collection("interacciones").stream())
    interacciones = [_normalizar_interaccion_doc(d.to_dict() or {}) for d in docs]

    n = len(interacciones)
    sentimientos = [float(i.get("sentimiento", 0) or 0) for i in interacciones]
    reacciones = [int(i.get("reacciones_recibidas", 0) or 0) for i in interacciones]
    textos = [i for i in interacciones if i.get("tipo") != "reaccion"]
    n_txt = len(textos)
    longitudes = [len((i.get("texto") or "")) for i in textos]

    por_canal = {c: 0 for c in CANALES_VALIDOS}
    por_tema: dict[str, int] = {}
    heatmap = {dia: {bloque: 0 for bloque in BLOQUES_HEATMAP} for dia in DIAS_HEATMAP}

    for i in interacciones:
        canal = i.get("canal")
        tema = i.get("tema") or "Sin tema"
        if canal in por_canal:
            por_canal[canal] += 1
        else:
            por_canal[canal] = por_canal.get(canal, 0) + 1
        por_tema[tema] = por_tema.get(tema, 0) + 1

        ts = i.get("timestamp")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                dt_utc = dt.astimezone(timezone.utc)
                dia = DIAS_HEATMAP[dt_utc.weekday()]
                bloque_idx = min(dt_utc.hour // 4, 5)
                bloque = BLOQUES_HEATMAP[bloque_idx]
                heatmap[dia][bloque] += 1
            except Exception:
                pass

    ultimas = sorted(interacciones, key=lambda x: str(x.get("timestamp", "")), reverse=True)[:10]
    ultimas_limpias = []
    for i in ultimas:
        ultimas_limpias.append({
            "id_interaccion": i.get("id_interaccion"),
            "id_usuario": i.get("id_usuario"),
            "timestamp": i.get("timestamp"),
            "canal": i.get("canal"),
            "tipo": i.get("tipo"),
            "tema": i.get("tema"),
            "sentimiento": i.get("sentimiento"),
            "reacciones_recibidas": i.get("reacciones_recibidas"),
            "texto": i.get("texto"),
        })

    metricas = {
        "total_interacciones": n,
        "sentimiento_promedio": round(sum(sentimientos) / n, 6) if n else 0.0,
        "reacciones_promedio": round(sum(reacciones) / n, 6) if n else 0.0,
        "longitud_media_texto": round(sum(longitudes) / n_txt, 6) if n_txt else 0.0,
        "total_textos": n_txt,
        "por_canal": por_canal,
        "por_tema": por_tema,
        "heatmap_actividad": heatmap,
        "ultimas_interacciones": ultimas_limpias,
        "ultima_actualizacion": utc_now_iso(),
        "formulas": {
            "total_interacciones": "N",
            "sentimiento_promedio": "S̄ = (1/N) * Σ s_i",
            "reacciones_promedio": "R̄ = (1/N) * Σ r_i",
            "longitud_media_texto": "L̄ = (1/N_txt) * Σ len(texto_i), excluyendo reacciones",
        },
    }

    if guardar_en_firestore:
        db.collection("metricas_tiempo_real").document("resumen_global").set(metricas)
    return metricas


async def emitir_evento_interaccion(evento: str, interaccion: dict | None, actor: str):
    metricas = recalcular_metricas_dinamicas(guardar_en_firestore=True)
    payload = {
        "type": evento,
        "evento": evento,
        "actor": actor,
        "timestamp_utc": utc_now_iso(),
        "interaccion": interaccion,
        "metricas": metricas,
    }
    await gestor_ws.broadcast(payload)


@app.get("/")
async def root():
    return RedirectResponse(url="/static/login.html")


@app.post("/api/auth/login")
async def login(req: LoginRequest):
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
    if not bcrypt.checkpw(req.password.encode("utf-8"), password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas")
    token = crear_jwt(user["username"], user.get("rol") or "admin")
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
async def listar_interacciones(canal: str | None = None, tema: str | None = None, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    docs = [_normalizar_interaccion_doc(d.to_dict() or {}) for d in db.collection("interacciones").stream()]
    if canal:
        docs = [d for d in docs if d.get("canal") == canal]
    if tema:
        docs = [d for d in docs if d.get("tema") == tema]
    docs.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return {"total": len(docs), "interacciones": docs}


@app.get("/api/interacciones/{id_interaccion}")
async def obtener_interaccion(id_interaccion: str, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    doc = db.collection("interacciones").document(id_interaccion).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")
    return _normalizar_interaccion_doc(doc.to_dict() or {})


@app.post("/api/interacciones", status_code=status.HTTP_201_CREATED)
async def crear_interaccion(payload: InteraccionBase, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    doc_ref = db.collection("interacciones").document(payload.id_interaccion)
    if doc_ref.get().exists:
        raise HTTPException(status_code=409, detail="id_interaccion ya existe en Firestore")

    res = ejecutar_d1("SELECT id_usuario FROM usuarios WHERE id_usuario = ? LIMIT 1", [payload.id_usuario])
    if not res.get("results"):
        raise HTTPException(status_code=400, detail="id_usuario no existe en Cloudflare D1 (usuarios)")

    data = payload.model_dump()
    if data["tipo"] == "reaccion":
        data["texto"] = None
        data["longitud_texto"] = 0
    else:
        data["longitud_texto"] = _calcular_longitud_texto(data["tipo"], data.get("texto"))

    data["auditoria"] = {
        "origen": "api_rest_postman",
        "fecha_carga_utc": utc_now_iso(),
        "regla_privacidad": "sin_datos_geo",
        "actor": usuario["username"],
    }
    # Garantizar ausencia de campos geo
    for k in ["pais", "estado", "ciudad", "latitud", "longitud"]:
        data.pop(k, None)

    doc_ref.set(data)
    await emitir_evento_interaccion("interaccion_creada", data, usuario["username"])
    return data


@app.put("/api/interacciones/{id_interaccion}")
async def actualizar_interaccion(id_interaccion: str, payload: InteraccionUpdate, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    doc_ref = db.collection("interacciones").document(id_interaccion)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")

    actual = _normalizar_interaccion_doc(doc.to_dict() or {})
    cambios = payload.model_dump(exclude_unset=True)
    if not cambios:
        raise HTTPException(status_code=400, detail="No se proporcionaron campos para actualizar")

    actualizado = {**actual, **cambios}
    actualizado["id_interaccion"] = actual.get("id_interaccion", id_interaccion)
    actualizado["id_usuario"] = actual.get("id_usuario")

    if actualizado.get("canal") not in CANALES_VALIDOS:
        raise HTTPException(status_code=400, detail="canal inválido")
    if actualizado.get("tipo") not in TIPOS_VALIDOS:
        raise HTTPException(status_code=400, detail="tipo inválido")
    try:
        sent = float(actualizado.get("sentimiento"))
    except Exception:
        raise HTTPException(status_code=400, detail="sentimiento inválido")
    if not -1.0 <= sent <= 1.0:
        raise HTTPException(status_code=400, detail="sentimiento debe estar en [-1.0, 1.0]")
    actualizado["sentimiento"] = sent

    if actualizado["tipo"] == "reaccion":
        actualizado["texto"] = None
        actualizado["longitud_texto"] = 0
    else:
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
    for k in ["pais", "estado", "ciudad", "latitud", "longitud"]:
        actualizado.pop(k, None)

    doc_ref.set(actualizado)
    await emitir_evento_interaccion("interaccion_actualizada", actualizado, usuario["username"])
    return actualizado


@app.delete("/api/interacciones/{id_interaccion}")
async def eliminar_interaccion(id_interaccion: str, usuario=Depends(obtener_usuario_actual)):
    db = get_firestore()
    doc_ref = db.collection("interacciones").document(id_interaccion)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Interacción no encontrada")
    data = _normalizar_interaccion_doc(doc.to_dict() or {})
    doc_ref.delete()
    await emitir_evento_interaccion("interaccion_eliminada", {"id_interaccion": id_interaccion, **data}, usuario["username"])
    return {"ok": True, "mensaje": "Interacción eliminada", "id_interaccion": id_interaccion}


@app.get("/api/historico/dashboard")
async def historico_dashboard(usuario=Depends(obtener_usuario_actual)):
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
        metricas = recalcular_metricas_dinamicas(guardar_en_firestore=False)
        await websocket.send_text(json.dumps({
            "type": "snapshot_inicial",
            "evento": "snapshot_inicial",
            "timestamp_utc": utc_now_iso(),
            "metricas": metricas,
        }, ensure_ascii=False, default=str))
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
