"""Crea o actualiza en Cloudflare D1 el usuario docente que pide la entrega final.

Uso:  python crear_usuario_docente.py
La contraseña se escribe en pantalla cuando el script la pide (no se guarda en ningún archivo).
"""
import getpass
import sys

import bcrypt

import setup_auth_d1 as auth

USUARIO = "lizandro.reyes"
CORREO = "lizandro.reyes@utmetropolitana.edu.mx"
NOMBRE = "Lizandro Reyes"


def asegurar_tabla():
    columnas = [c[0] for c in auth.columnas_actuales_usuarios_sistema()]
    if not columnas:
        auth.crear_tabla_usuarios_sistema()
        print("[OK] Tabla usuarios_sistema creada")
    elif "correo" not in columnas:
        auth.ejecutar_d1("ALTER TABLE usuarios_sistema ADD COLUMN correo TEXT;")
        print("[OK] Columna correo agregada (sin borrar usuarios)")
    auth.ejecutar_d1(auth.INDICE_CORREO)


def main():
    clave = getpass.getpass(f"Contraseña para {USUARIO}: ")
    if clave != getpass.getpass("Escríbela otra vez: "):
        sys.exit("[ERROR] Las contraseñas no coinciden; no se cambió nada.")
    if clave != clave.strip():
        sys.exit("[ERROR] La contraseña tiene espacios al inicio o al final; no se cambió nada.")
    asegurar_tabla()
    auth.registrar_o_actualizar_admin(
        username=USUARIO, nombre_completo=NOMBRE, password=clave, rol="docente", activo=1, correo=CORREO
    )
    fila = auth.ejecutar_d1(
        "SELECT username, correo, rol, activo, password_hash FROM usuarios_sistema WHERE username = ? LIMIT 1",
        [USUARIO],
    ).get("results", [])
    if not fila:
        sys.exit("[ERROR] No se encontró el usuario después de guardarlo.")
    fila = fila[0]
    coincide = bcrypt.checkpw(clave.encode("utf-8"), fila["password_hash"].encode("utf-8"))
    print("[VERIFICACION]", {k: fila[k] for k in ("username", "correo", "rol", "activo")}, "| contraseña guardada como hash bcrypt:", coincide)


if __name__ == "__main__":
    main()
