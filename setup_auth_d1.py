import sys

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

import os
from datetime import datetime, timezone

import bcrypt
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

ESQUEMA_USUARIOS_SISTEMA = """
CREATE TABLE IF NOT EXISTS usuarios_sistema (
    id_admin INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    nombre_completo TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    rol TEXT NOT NULL DEFAULT 'administrador',
    activo INTEGER NOT NULL DEFAULT 1,
    creado_utc TEXT NOT NULL,
    correo TEXT
)
"""

# El correo es opcional, pero no se puede repetir: así el login por correo identifica a un solo usuario.
INDICE_CORREO = "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_sistema_correo ON usuarios_sistema (correo)"

COLUMNAS_ESPERADAS = (
    ("id_admin", "INTEGER", 0, 1),
    ("username", "TEXT", 1, 0),
    ("nombre_completo", "TEXT", 1, 0),
    ("password_hash", "TEXT", 1, 0),
    ("rol", "TEXT", 1, 0),
    ("activo", "INTEGER", 1, 0),
    ("creado_utc", "TEXT", 1, 0),
    ("correo", "TEXT", 0, 0),
)

# Las credenciales del administrador se leen del archivo .env (nunca se escriben en el código).
DEFAULT_ADMIN = {
    "username": os.getenv("ADMIN_USERNAME") or "admin_c360",
    "nombre_completo": os.getenv("ADMIN_NOMBRE_COMPLETO") or "José Manuel González Canché",
    "password": os.getenv("ADMIN_PASSWORD"),
    "rol": "administrador",
    "activo": 1,
}


def _env(nombre):
    valor = os.getenv(nombre)
    if not valor:
        raise RuntimeError(f"Falta variable de entorno: {nombre}")
    return valor


def ejecutar_d1(sql, params=None):
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{_env('CLOUDFLARE_ACCOUNT_ID')}/d1/database/"
        f"{_env('CLOUDFLARE_DATABASE_ID')}/query"
    )
    headers = {
        "Authorization": f"Bearer {_env('CLOUDFLARE_API_TOKEN')}",
        "Content-Type": "application/json",
    }
    payload = {"sql": sql}
    if params is not None:
        payload["params"] = params
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"D1 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 rechazó la consulta: {data.get('errors')}")
    return data["result"][0]


def crear_tabla_usuarios_sistema():
    ejecutar_d1(ESQUEMA_USUARIOS_SISTEMA)


def columnas_actuales_usuarios_sistema():
    pragma = ejecutar_d1("PRAGMA table_info(usuarios_sistema);")
    return tuple(
        (
            fila.get("name"),
            str(fila.get("type") or "").upper(),
            int(fila.get("notnull") or 0),
            int(fila.get("pk") or 0),
        )
        for fila in pragma.get("results", [])
    )


def verificar_o_recrear_tabla_usuarios_sistema():
    columnas = columnas_actuales_usuarios_sistema()
    if columnas == COLUMNAS_ESPERADAS[:-1]:
        # Tabla de la versión anterior (sin correo): se agrega la columna sin borrar a los usuarios.
        ejecutar_d1("ALTER TABLE usuarios_sistema ADD COLUMN correo TEXT;")
        print("[OK] Columna correo agregada a usuarios_sistema")
        columnas = columnas_actuales_usuarios_sistema()
    if columnas == COLUMNAS_ESPERADAS:
        ejecutar_d1(INDICE_CORREO)
        return False
    print("[INFO] Esquema de usuarios_sistema distinto del esperado; se recrea la tabla")
    ejecutar_d1("DROP TABLE IF EXISTS usuarios_sistema;")
    crear_tabla_usuarios_sistema()
    ejecutar_d1(INDICE_CORREO)
    return True


def registrar_o_actualizar_admin(username=None, nombre_completo=None, password=None, rol="administrador", activo=1, correo=None):
    username = (username or DEFAULT_ADMIN["username"]).strip()
    correo = (correo or "").strip().lower() or None
    nombre_completo = (nombre_completo or DEFAULT_ADMIN["nombre_completo"]).strip()
    password = password or DEFAULT_ADMIN["password"]
    if not username or not password:
        raise RuntimeError("Define ADMIN_USERNAME y ADMIN_PASSWORD en el archivo .env antes de ejecutar este script")
    if len(password) < 12:
        raise RuntimeError("ADMIN_PASSWORD debe tener al menos 12 caracteres")

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    creado_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    existente = ejecutar_d1(
        "SELECT id_admin FROM usuarios_sistema WHERE username = ? LIMIT 1",
        [username],
    )
    rows = existente.get("results", [])
    if rows:
        ejecutar_d1(
            """
            UPDATE usuarios_sistema
            SET nombre_completo = ?, password_hash = ?, rol = ?, activo = ?, correo = COALESCE(?, correo)
            WHERE username = ?
            """,
            [nombre_completo, password_hash, rol, int(activo), correo, username],
        )
        print(f"[OK] Usuario actualizado: {username}")
    else:
        ejecutar_d1(
            """
            INSERT INTO usuarios_sistema (username, nombre_completo, password_hash, rol, activo, creado_utc, correo)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [username, nombre_completo, password_hash, rol, int(activo), creado_utc, correo],
        )
        print(f"[OK] Usuario creado: {username}")


def main():
    if not DEFAULT_ADMIN["password"]:
        raise RuntimeError("Define ADMIN_USERNAME y ADMIN_PASSWORD en el archivo .env antes de ejecutar este script")
    recreada = verificar_o_recrear_tabla_usuarios_sistema()
    if recreada:
        print("[OK] Tabla usuarios_sistema recreada en D1")
    else:
        print("[OK] Tabla usuarios_sistema verificada en D1")
    registrar_o_actualizar_admin()
    verificacion = ejecutar_d1(
        "SELECT id_admin, username, correo, nombre_completo, rol, activo, creado_utc FROM usuarios_sistema WHERE username = ?",
        [DEFAULT_ADMIN["username"]],
    )
    rows = verificacion.get("results", [])
    if not rows:
        raise RuntimeError("No se pudo verificar el usuario administrador")
    row = rows[0]
    print("[VERIFICACION]", {k: row.get(k) for k in ["id_admin", "username", "correo", "nombre_completo", "rol", "activo", "creado_utc"]})


if __name__ == "__main__":
    main()
