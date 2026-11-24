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

### Autenticación
- `POST /api/auth/login`: valida credenciales en D1 y devuelve JWT.
- `GET /api/auth/verify`: valida sesión activa.
- `POST /api/auth/logout`: revoca el token activo en memoria.

### Interacciones (Firestore)
- `GET /api/interacciones`
- `GET /api/interacciones/{id_interaccion}`
- `POST /api/interacciones`
- `PUT /api/interacciones/{id_interaccion}`
- `DELETE /api/interacciones/{id_interaccion}`

### Históricos (Cloudflare D1)
- `GET /api/historico/dashboard`

### WebSocket
- `WS /ws?token=<jwt>`: envía snapshot inicial y eventos de creación/actualización/eliminación con métricas recalculadas.

## Notas de privacidad

- Las interacciones almacenadas desde API incluyen auditoría con `regla_privacidad = sin_datos_geo`.
- El dashboard histórico consulta D1 y filtra ubicaciones autorizadas con `consentimiento_datos = 1`.
- Las operaciones CRUD de interacciones se realizan desde Postman; el dashboard no incluye formularios CRUD.
