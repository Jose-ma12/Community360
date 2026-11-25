# Community360

Plataforma de analítica comunitaria con backend FastAPI, autenticación JWT, Cloudflare D1, Firebase Firestore y dashboard en tiempo real.

## Estructura principal

- `setup_auth_d1.py`: crea/verifica la tabla `usuarios_sistema` en Cloudflare D1 y registra el usuario administrador con contraseña cifrada con bcrypt.
- `main.py`: API FastAPI con autenticación JWT, endpoints REST, WebSockets y servicio de archivos estáticos.
- `static/`: frontend responsivo (`login.html`, `dashboard.html`, `app.js`, `styles.css`).
- `Community360_Postman_Collection.json`: colección Postman v2.1 para pruebas de autenticación y CRUD.

## Requisitos

- Python 3.11+
- Plantilla de configuración: `.env.example` (copiar a `.env` y completar sin versionar los valores reales)
- Variables en `.env`: `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`, `FIREBASE_CREDENTIALS_PATH`, `JWT_SECRET_KEY`, `JWT_EXPIRE_MINUTES`
- Credenciales Firebase en el archivo indicado por `FIREBASE_CREDENTIALS_PATH`

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Después de copiar la plantilla, completa `.env` con tus valores reales. Tanto `.env` como `firebase_credentials.json` están excluidos por `.gitignore`.

## Inicialización de autenticación (Fase 3)

```bash
python setup_auth_d1.py
```

Crea la tabla `usuarios_sistema` y registra/actualiza el usuario administrador configurado en el script.

## Ejecución del backend

```bash
uvicorn main:app --reload
```

- Login: `http://127.0.0.1:8000/static/login.html`
- Dashboard: `http://127.0.0.1:8000/static/dashboard.html`

## Endpoints principales

### Estado del servicio
- `GET /api/health`: sin autenticación; comprueba la conexión con D1 y Firestore. Responde 200 si ambos están disponibles y 503 si alguno falla, indicando cuál.

### Autenticación
- `POST /api/auth/login`: valida credenciales en D1 y devuelve JWT.
- `GET /api/auth/verify`: valida sesión activa.
- `POST /api/auth/logout`: revoca el token activo. La revocación se guarda en la tabla `tokens_revocados` de D1 (identificada por el `jti` del token), así que el token sigue invalidado aunque se reinicie el servidor. Las revocaciones de tokens ya expirados se depuran automáticamente.

### Interacciones (Firestore)
- `GET /api/interacciones`: filtros opcionales `canal`, `tema`, `tipo` e `id_usuario`; paginación con `limit` (1–500, por defecto 100) y `offset` (por defecto 0). La respuesta incluye `total`, `limit`, `offset` e `interacciones`.
- `GET /api/interacciones/{id_interaccion}`
- `POST /api/interacciones`
- `PUT /api/interacciones/{id_interaccion}`
- `DELETE /api/interacciones/{id_interaccion}`

### Históricos (Cloudflare D1)
- `GET /api/historico/dashboard`
- `GET /api/historico/usuarios`: padrón consolidado (usuarios + métricas). Filtros opcionales `plataforma`, `interes`, `estatus` y `es_artista` (0 o 1); paginación con `limit` (1–200, por defecto 50) y `offset`. La ubicación (`pais`, `estado`, `ciudad`) solo aparece para usuarios con `consentimiento_datos = 1`.
- `GET /api/historico/usuarios/{id_usuario}`: ficha de un usuario con la misma regla de privacidad; 400 si el id no cumple `^U\d+$`, 404 si no existe.
- `GET /api/historico/cuarentena`: interacciones rechazadas por el ETL, con conteo `por_motivo`. Filtro opcional `motivo`; paginación con `limit` (1–200, por defecto 50) y `offset`.

### WebSocket
- `WS /ws?token=<jwt>`: envía snapshot inicial y eventos de creación/actualización/eliminación con métricas recalculadas.

## Notas de privacidad

- Las interacciones almacenadas desde API incluyen auditoría con `regla_privacidad = sin_datos_geo`.
- El dashboard histórico consulta D1 y filtra ubicaciones autorizadas con `consentimiento_datos = 1`.
- Las operaciones CRUD de interacciones se realizan desde Postman; el dashboard no incluye formularios CRUD.
