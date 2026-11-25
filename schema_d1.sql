-- Esquema relacional de Community360 en Cloudflare D1 (SQLite).
-- datos.py lo ejecuta antes de cargar; IF NOT EXISTS evita tocar tablas que ya existen.
-- La tabla usuarios_sistema (login) la crea setup_auth_d1.py.

CREATE TABLE IF NOT EXISTS usuarios (
    id_usuario TEXT PRIMARY KEY CHECK (id_usuario GLOB 'U[0-9]*'),
    alias TEXT NOT NULL UNIQUE,
    fecha_ingreso TEXT NOT NULL,
    rango_edad TEXT NOT NULL CHECK (rango_edad IN ('18-24', '25-31', '32-38', '39+')),
    especie_fursona TEXT NOT NULL,
    interes_principal TEXT NOT NULL,
    plataforma_principal TEXT NOT NULL CHECK (
        plataforma_principal IN ('Telegram', 'Facebook', 'Instagram', 'TikTok', 'Twitter/X', 'Discord')
    ),
    es_artista INTEGER NOT NULL CHECK (es_artista IN (0, 1)),
    consentimiento_datos INTEGER NOT NULL CHECK (consentimiento_datos IN (0, 1))
);

CREATE TABLE IF NOT EXISTS usuarios_ubicacion (
    id_usuario TEXT PRIMARY KEY REFERENCES usuarios (id_usuario),
    pais TEXT NOT NULL,
    estado TEXT NOT NULL,
    ciudad TEXT NOT NULL,
    latitud REAL CHECK (latitud IS NULL OR latitud BETWEEN -90 AND 90),
    longitud REAL CHECK (longitud IS NULL OR longitud BETWEEN -180 AND 180),
    zona_horaria TEXT
);

CREATE TABLE IF NOT EXISTS usuarios_metricas (
    id_usuario TEXT PRIMARY KEY REFERENCES usuarios (id_usuario),
    mensajes_30d INTEGER NOT NULL CHECK (mensajes_30d >= 0),
    reacciones_30d INTEGER NOT NULL CHECK (reacciones_30d >= 0),
    eventos_asistidos INTEGER NOT NULL CHECK (eventos_asistidos >= 0),
    comisiones_compradas INTEGER NOT NULL CHECK (comisiones_compradas >= 0),
    sentimiento_promedio REAL NOT NULL CHECK (sentimiento_promedio BETWEEN -1.0 AND 1.0),
    ultima_actividad TEXT NOT NULL,
    estatus TEXT NOT NULL CHECK (estatus IN ('activo', 'en_riesgo', 'inactivo'))
);

CREATE TABLE IF NOT EXISTS interacciones_cuarentena (
    id_cuarentena INTEGER PRIMARY KEY AUTOINCREMENT,
    id_interaccion TEXT NOT NULL,
    id_usuario_origen TEXT NOT NULL,
    timestamp_origen TEXT NOT NULL,
    canal TEXT NOT NULL,
    tipo TEXT NOT NULL,
    tema TEXT NOT NULL,
    motivo_rechazo TEXT NOT NULL,
    fecha_registro TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cuarentena_usuario ON interacciones_cuarentena (id_usuario_origen);

CREATE TABLE IF NOT EXISTS tokens_revocados (
    jti TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expira_epoch INTEGER NOT NULL,
    revocado_utc TEXT NOT NULL
);
