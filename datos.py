import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
import pandas as pd
import requests
from dotenv import load_dotenv
from firebase_admin import credentials, firestore

from metricas import (
    CANALES_VALIDOS,
    DISPOSITIVOS_VALIDOS,
    TEMAS_VALIDOS,
    TIPOS_VALIDOS,
    calcular_metricas as calcular_metricas_globales,
)

BASE_DIR = Path(__file__).resolve().parent
ARCHIVO_USUARIOS = BASE_DIR / "comunidad_virtual_usuarios.csv"
ARCHIVO_INTERACCIONES = BASE_DIR / "comunidad_virtual_1000_interacciones.csv"
DIR_SALIDA = BASE_DIR / "datos_depurados"
ARCHIVO_ESQUEMA_D1 = BASE_DIR / "schema_d1.sql"
TIMESTAMP_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def cargar_credenciales():
    load_dotenv(BASE_DIR / ".env")
    requeridas = [
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_DATABASE_ID",
        "CLOUDFLARE_API_TOKEN",
    ]
    faltantes = [nombre for nombre in requeridas if not os.getenv(nombre)]
    if faltantes:
        raise RuntimeError(f"Variables de entorno faltantes: {', '.join(faltantes)}")
    cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        try:
            json.loads(cred_json)
        except json.JSONDecodeError as exc:
            raise ValueError("FIREBASE_CREDENTIALS_JSON no contiene un JSON válido") from exc
        return
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
    if not cred_path:
        raise RuntimeError("Falta FIREBASE_CREDENTIALS_JSON o FIREBASE_CREDENTIALS_PATH")
    ruta_credenciales = BASE_DIR / cred_path
    if not ruta_credenciales.is_file():
        raise FileNotFoundError("No se encontró el archivo de credenciales de Firebase")


def forzar_object(dataframe):
    for columna in dataframe.columns:
        dataframe[columna] = pd.Series(dataframe[columna].tolist(), index=dataframe.index, dtype=object)
    return dataframe


def nulo(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    texto = str(valor).strip()
    return texto if texto else None


def valor_json(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    return valor


def a_iso_fecha(valor):
    texto = nulo(valor)
    if texto is None:
        return None
    fecha = datetime.strptime(texto, "%d/%m/%Y").date()
    return fecha.isoformat()


def a_entero(valor, columna, minimo=0, maximo=None):
    texto = nulo(valor)
    if texto is None:
        return None
    numero = int(texto)
    if numero < minimo or (maximo is not None and numero > maximo):
        raise ValueError(f"{columna}={numero} fuera de rango")
    return numero


def a_flotante(valor, columna, minimo, maximo):
    texto = nulo(valor)
    if texto is None:
        return None
    numero = float(texto)
    if not minimo <= numero <= maximo:
        raise ValueError(f"{columna}={numero} fuera de rango")
    return numero


def a_etiquetas(valor):
    texto = nulo(valor)
    if texto is None:
        return []
    return [parte.lower() for parte in (p.strip() for p in texto.split("|")) if parte]


def etapa_1_deduplicar(usuarios, interacciones, bitacora):
    for df, columnas in ((usuarios, ["id_usuario", "alias"]), (interacciones, ["id_interaccion"])):
        for columna in columnas:
            df[columna] = df[columna].map(lambda v: nulo(v) or "").str.strip()

    usuarios["_alias_lower"] = usuarios["alias"].str.lower()
    usuarios = usuarios.drop_duplicates()
    usuarios = usuarios.drop_duplicates(subset=["id_usuario"], keep="first")
    usuarios = usuarios.drop_duplicates(subset=["_alias_lower"], keep="first")
    usuarios = usuarios.drop(columns=["_alias_lower"])

    interacciones = interacciones.drop_duplicates()
    interacciones = interacciones.drop_duplicates(subset=["id_interaccion"], keep="first")

    bitacora["etapa_1_duplicados"] = {
        "usuarios_antes": bitacora["lectura_inicial"]["usuarios"],
        "usuarios_despues": len(usuarios),
        "interacciones_antes": bitacora["lectura_inicial"]["interacciones"],
        "interacciones_despues": len(interacciones),
    }
    print(
        "[ETAPA 1] Duplicados: usuarios "
        f"{bitacora['etapa_1_duplicados']['usuarios_antes']} -> {len(usuarios)}, "
        f"interacciones {bitacora['etapa_1_duplicados']['interacciones_antes']} -> {len(interacciones)}"
    )
    return usuarios.reset_index(drop=True), interacciones.reset_index(drop=True)


def etapa_2_vacios(usuarios, interacciones, bitacora):
    total_usuarios = int(usuarios.map(lambda v: isinstance(v, str) and not v.strip()).sum().sum())
    celdas_interacciones = int(interacciones.map(lambda v: isinstance(v, str) and not v.strip()).sum().sum())
    usuarios = usuarios.map(nulo)
    interacciones = interacciones.map(nulo)
    es_reaccion = [str(v).strip().lower() == "reaccion" for v in interacciones["tipo"]]
    interacciones["texto"] = [None if flag else v for flag, v in zip(es_reaccion, interacciones["texto"])]
    interacciones["longitud_texto"] = [
        0 if flag else v for flag, v in zip(es_reaccion, interacciones["longitud_texto"])
    ]
    bitacora["etapa_2_vacios"] = {
        "usuario_celdas_vacias_a_none": total_usuarios,
        "interaccion_celdas_vacias_a_none": celdas_interacciones,
        "reacciones_con_texto_forzado": sum(es_reaccion),
    }
    print(
        "[ETAPA 2] Vacíos normalizados y "
        f"{bitacora['etapa_2_vacios']['reacciones_con_texto_forzado']} reacciones sin texto"
    )
    return usuarios, interacciones


def etapa_3_formatos(usuarios, interacciones, bitacora):
    for columna in ["fecha_ingreso", "ultima_actividad"]:
        usuarios[columna] = usuarios[columna].map(a_iso_fecha)

    timestamps_validos = interacciones["timestamp"].map(
        lambda v: isinstance(v, str) and bool(TIMESTAMP_UTC_RE.match(v))
    )
    if not timestamps_validos.all():
        raise ValueError(f"Timestamps ISO-8601 UTC inválidos: {(~timestamps_validos).sum()}")

    usuarios["consentimiento_datos"] = usuarios["consentimiento_datos"].map(
        lambda v: {"si": 1, "no": 0}[str(v).strip().lower()]
    )
    interacciones["etiquetas"] = interacciones["etiquetas"].map(a_etiquetas)
    bitacora["etapa_3_formatos"] = {
        "fechas_convertidas": len(usuarios) * 2,
        "timestamps_validados": len(interacciones),
        "consentimiento_si": int((usuarios["consentimiento_datos"] == 1).sum()),
        "consentimiento_no": int((usuarios["consentimiento_datos"] == 0).sum()),
        "interacciones_con_etiquetas": int(interacciones["etiquetas"].map(len).gt(0).sum()),
    }
    print(
        "[ETAPA 3] Fechas ISO, timestamps UTC validados y "
        f"{bitacora['etapa_3_formatos']['interacciones_con_etiquetas']} registros con etiquetas"
    )
    return usuarios, interacciones


def etapa_4_validar(usuarios, interacciones, bitacora):
    enteros = {
        "es_artista": (0, 1),
        "mensajes_30d": (0, None),
        "reacciones_30d": (0, None),
        "eventos_asistidos": (0, None),
        "comisiones_compradas": (0, None),
    }
    for columna, (minimo, maximo) in enteros.items():
        usuarios[columna] = usuarios[columna].map(
            lambda v, c=columna, mn=minimo, mx=maximo: a_entero(v, c, mn, mx)
        )
    usuarios["latitud"] = usuarios["latitud"].map(lambda v: a_flotante(v, "latitud", -90.0, 90.0))
    usuarios["longitud"] = usuarios["longitud"].map(lambda v: a_flotante(v, "longitud", -180.0, 180.0))
    usuarios["sentimiento_promedio"] = usuarios["sentimiento_promedio"].map(
        lambda v: a_flotante(v, "sentimiento_promedio", -1.0, 1.0)
    )
    interacciones["longitud_texto"] = interacciones["longitud_texto"].map(
        lambda v: a_entero(v, "longitud_texto")
    )
    interacciones["reacciones_recibidas"] = interacciones["reacciones_recibidas"].map(
        lambda v: a_entero(v, "reacciones_recibidas")
    )
    interacciones["sentimiento"] = interacciones["sentimiento"].map(
        lambda v: a_flotante(v, "sentimiento", -1.0, 1.0)
    )

    catalogos = {
        "canal": CANALES_VALIDOS,
        "tipo": TIPOS_VALIDOS,
        "tema": TEMAS_VALIDOS,
        "dispositivo": DISPOSITIVOS_VALIDOS,
    }
    for columna, permitidos in catalogos.items():
        fuera = ~interacciones[columna].isin(permitidos)
        if fuera.any():
            valores = sorted(set(map(str, interacciones.loc[fuera, columna])))
            raise ValueError(f"{columna} con valores fuera de catálogo: {valores}")

    fechas_ordenadas = usuarios["ultima_actividad"] >= usuarios["fecha_ingreso"]
    if not fechas_ordenadas.all():
        raise ValueError(f"ultima_actividad < fecha_ingreso en {(~fechas_ordenadas).sum()} usuarios")
    bitacora["etapa_4_validacion"] = {
        "usuarios_validados": len(usuarios),
        "interacciones_validadas": len(interacciones),
        "fechas_cronologicamente_validas": int(fechas_ordenadas.sum()),
        "categorias_validadas": list(catalogos),
    }
    print(
        "[ETAPA 4] Tipos y rangos validados: "
        f"{len(usuarios)} usuarios, {len(interacciones)} interacciones"
    )
    return usuarios, interacciones


def etapa_5_integridad(usuarios, interacciones, bitacora):
    ids_validos = set(usuarios["id_usuario"])
    es_valida = interacciones["id_usuario"].isin(ids_validos)
    validas = interacciones[es_valida].reset_index(drop=True).copy()
    cuarentena = interacciones[~es_valida].reset_index(drop=True).copy()
    cuarentena = cuarentena.rename(columns={"id_usuario": "id_usuario_origen", "timestamp": "timestamp_origen"})
    cuarentena["motivo_rechazo"] = "FK_USUARIO_INEXISTENTE"
    bitacora["etapa_5_integridad"] = {
        "interacciones_validas": len(validas),
        "interacciones_huerfanas": len(cuarentena),
        "motivo_rechazo": "FK_USUARIO_INEXISTENTE",
    }
    print(
        "[ETAPA 5] Integridad referencial: "
        f"{len(validas)} válidas, {len(cuarentena)} en cuarentena"
    )
    return validas, cuarentena


def etapa_6_privacidad(usuarios, bitacora):
    maestros = usuarios[
        [
            "id_usuario", "alias", "fecha_ingreso", "rango_edad", "especie_fursona",
            "interes_principal", "plataforma_principal", "es_artista", "consentimiento_datos",
        ]
    ].copy()
    ubicacion = usuarios.loc[
        usuarios["consentimiento_datos"] == 1,
        ["id_usuario", "pais", "estado", "ciudad", "latitud", "longitud", "zona_horaria"],
    ].copy()
    metricas = usuarios[
        [
            "id_usuario", "mensajes_30d", "reacciones_30d", "eventos_asistidos",
            "comisiones_compradas", "sentimiento_promedio", "ultima_actividad", "estatus",
        ]
    ].copy()

    sin_consentimiento = usuarios[usuarios["consentimiento_datos"] == 0].iloc[0]
    ejemplo = {
        "id_usuario": sin_consentimiento["id_usuario"],
        "consentimiento_datos": int(sin_consentimiento["consentimiento_datos"]),
        "antes": {
            "pais": valor_json(sin_consentimiento["pais"]),
            "estado": valor_json(sin_consentimiento["estado"]),
            "ciudad": valor_json(sin_consentimiento["ciudad"]),
            "latitud": valor_json(sin_consentimiento["latitud"]),
            "longitud": valor_json(sin_consentimiento["longitud"]),
        },
        "despues": {
            "existe_en_usuarios_ubicacion": sin_consentimiento["id_usuario"] in set(ubicacion["id_usuario"]),
            "campos_geo_en_firestore": [],
            "regla_privacidad": "sin_datos_geo",
        },
    }
    bitacora["etapa_6_privacidad"] = {
        "usuarios": len(maestros),
        "usuarios_ubicacion": len(ubicacion),
        "usuarios_metricas": len(metricas),
        "usuarios_sin_geo": int((usuarios["consentimiento_datos"] == 0).sum()),
        "ejemplo_sin_consentimiento": ejemplo,
    }
    print(
        "[ETAPA 6] Privacidad geográfica: "
        f"{len(ubicacion)}/{len(maestros)} usuarios con ubicación; regla sin_datos_geo activa"
    )
    return maestros, ubicacion, metricas


def calcular_metricas(validas):
    # Mismo cálculo que usa la API (metricas.py), para que resumen_global no cambie de forma.
    return calcular_metricas_globales(validas.to_dict("records"))


def etapa_7_exportar(maestros, ubicacion, metricas, cuarentena, validas, metricas_tiempo_real, bitacora):
    DIR_SALIDA.mkdir(exist_ok=True)
    maestros.to_csv(DIR_SALIDA / "usuarios_depurados.csv", index=False, encoding="utf-8")
    ubicacion.to_csv(DIR_SALIDA / "usuarios_ubicacion_depurados.csv", index=False, encoding="utf-8")
    metricas.to_csv(DIR_SALIDA / "usuarios_metricas_depurados.csv", index=False, encoding="utf-8")
    # Los archivos de interacciones no llevan país/estado/ciudad: en cuarentena no se puede comprobar
    # el consentimiento del usuario, y en las válidas la ubicación autorizada ya vive en usuarios_ubicacion.
    geo = ["pais", "estado", "ciudad", "latitud", "longitud", "zona_horaria"]
    cuarentena.drop(columns=geo, errors="ignore").to_csv(
        DIR_SALIDA / "interacciones_cuarentena.csv", index=False, encoding="utf-8"
    )
    validas.drop(columns=geo, errors="ignore").to_json(
        DIR_SALIDA / "interacciones_depuradas.json", orient="records", force_ascii=False, indent=2
    )
    (DIR_SALIDA / "metricas_tiempo_real.json").write_text(
        json.dumps(metricas_tiempo_real, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DIR_SALIDA / "bitacora_etl.json").write_text(
        json.dumps(bitacora, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("[ETAPA 7] Auditoría local exportada en datos_depurados/ (7 archivos)")


def ejecutar_d1(sql, params=None):
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{os.getenv('CLOUDFLARE_ACCOUNT_ID')}/d1/database/"
        f"{os.getenv('CLOUDFLARE_DATABASE_ID')}/query"
    )
    headers = {"Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN')}", "Content-Type": "application/json"}
    payload = {"sql": sql}
    if params:
        payload["params"] = params
    respuesta = requests.post(url, headers=headers, json=payload, timeout=60)
    if respuesta.status_code >= 400:
        raise RuntimeError(f"D1 HTTP {respuesta.status_code}: {respuesta.text[:300]}")
    datos = respuesta.json()
    if not datos.get("success"):
        raise RuntimeError(f"D1 rechazó la consulta: {datos.get('errors')}")
    return datos["result"][0]


def crear_esquema_d1():
    sql = ARCHIVO_ESQUEMA_D1.read_text(encoding="utf-8")
    sin_comentarios = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    sentencias = [x.strip() for x in sin_comentarios.split(";") if x.strip()]
    for sentencia in sentencias:
        ejecutar_d1(sentencia)
    return len(sentencias)


def insertar_d1_tabla(tabla, columnas, registros, tamanio_lote=10):
    marcas = "(" + ", ".join("?" for _ in columnas) + ")"
    for inicio in range(0, len(registros), tamanio_lote):
        lote = registros[inicio : inicio + tamanio_lote]
        params = []
        for fila in lote:
            params.extend(None if v is None or (isinstance(v, float) and pd.isna(v)) else v for v in fila)
        if len(params) > 100:
            raise ValueError(f"Lote excede el límite de 100 parámetros: {len(params)}")
        sql = (
            f"INSERT OR REPLACE INTO {tabla} ({', '.join(columnas)}) VALUES "
            + ", ".join([marcas] * len(lote))
        )
        ejecutar_d1(sql, params)
    return len(registros)


def etapa_8_carga(maestros, ubicacion, metricas, cuarentena, validas, metricas_tiempo_real, bitacora):
    sentencias = crear_esquema_d1()
    print(f"[ETAPA 8] Esquema D1 verificado ({sentencias} sentencias de schema_d1.sql)")
    ejecutar_d1("DELETE FROM interacciones_cuarentena")
    tablas = {
        "usuarios": (
            ["id_usuario", "alias", "fecha_ingreso", "rango_edad", "especie_fursona",
             "interes_principal", "plataforma_principal", "es_artista", "consentimiento_datos"],
            maestros,
        ),
        "usuarios_ubicacion": (
            ["id_usuario", "pais", "estado", "ciudad", "latitud", "longitud", "zona_horaria"],
            ubicacion,
        ),
        "usuarios_metricas": (
            ["id_usuario", "mensajes_30d", "reacciones_30d", "eventos_asistidos",
             "comisiones_compradas", "sentimiento_promedio", "ultima_actividad", "estatus"],
            metricas,
        ),
        "interacciones_cuarentena": (
            ["id_interaccion", "id_usuario_origen", "timestamp_origen", "canal", "tipo", "tema", "motivo_rechazo"],
            cuarentena,
        ),
    }
    conteos_d1 = {}
    for tabla, (columnas, dataframe) in tablas.items():
        registros = dataframe[columnas].itertuples(index=False, name=None)
        conteos_d1[tabla] = insertar_d1_tabla(tabla, columnas, list(registros))
    print("[ETAPA 8] D1 cargado: " + ", ".join(f"{k}={v}" for k, v in conteos_d1.items()))

    if not firebase_admin._apps:
        cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
        if cred_json:
            certificado = credentials.Certificate(json.loads(cred_json))
        else:
            certificado = credentials.Certificate(str(BASE_DIR / os.getenv("FIREBASE_CREDENTIALS_PATH")))
        firebase_admin.initialize_app(certificado)
    db = firestore.client()
    campos_firestore = [
        "id_interaccion", "id_usuario", "timestamp", "canal", "tipo", "texto", "tema",
        "sentimiento", "idioma", "dispositivo", "longitud_texto", "reacciones_recibidas", "etiquetas",
    ]
    fecha_carga = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    batch = db.batch()
    for indice, fila in enumerate(validas[campos_firestore].to_dict("records")):
        fila["auditoria"] = {
            "origen": "etl_inicial",
            "fecha_carga_utc": fecha_carga,
            "regla_privacidad": "sin_datos_geo",
        }
        batch.set(db.collection("interacciones").document(fila["id_interaccion"]), fila)
        if (indice + 1) % 450 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    db.collection("metricas_tiempo_real").document("resumen_global").set(metricas_tiempo_real)
    print(f"[ETAPA 8] Firestore cargado: interacciones={len(validas)}, resumen_global=1")

    bitacora["etapa_8_carga"] = {
        "d1": conteos_d1,
        "firestore_interacciones": len(validas),
        "firestore_resumen_global": 1,
        "fecha_carga_utc": fecha_carga,
        "regla_privacidad": "sin_datos_geo",
        "campos_geo_omitidos": ["pais", "estado", "ciudad", "latitud", "longitud"],
    }


def verificar(bitacora):
    conteos_d1 = {}
    for tabla in ["usuarios", "usuarios_ubicacion", "usuarios_metricas", "interacciones_cuarentena"]:
        conteos_d1[tabla] = ejecutar_d1(f"SELECT COUNT(*) AS total FROM {tabla}")["results"][0]["total"]
    db = firestore.client()
    total_firestore = sum(1 for _ in db.collection("interacciones").select("id_interaccion").stream())
    doc = db.collection("metricas_tiempo_real").document("resumen_global").get()
    esperados = bitacora["etapa_8_carga"]
    resultado = {
        "d1": conteos_d1,
        "d1_esperado": esperados["d1"],
        "firestore_interacciones": total_firestore,
        "firestore_esperado": esperados["firestore_interacciones"],
        "firestore_resumen_global_existe": doc.exists,
        "coincide": (
            all(conteos_d1[t] == esperados["d1"][t] for t in conteos_d1)
            and total_firestore == esperados["firestore_interacciones"]
            and doc.exists
        ),
    }
    (DIR_SALIDA / "verificacion_final.json").write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[VERIFICACIÓN] D1: " + ", ".join(f"{k}={v}" for k, v in conteos_d1.items()))
    print(f"[VERIFICACIÓN] Firestore interacciones={total_firestore}, resumen_global={doc.exists}")
    print(f"[VERIFICACIÓN] Coincide con bitácora: {resultado['coincide']}")
    if not resultado["coincide"]:
        raise RuntimeError("Los conteos en la nube no coinciden con la bitácora")


def main():
    cargar_credenciales()
    for archivo in (ARCHIVO_USUARIOS, ARCHIVO_INTERACCIONES):
        if not archivo.is_file():
            raise FileNotFoundError(f"Falta el archivo de datos: {archivo.name}")

    usuarios = forzar_object(pd.read_csv(ARCHIVO_USUARIOS, dtype=str, encoding="utf-8", keep_default_na=False))
    interacciones = forzar_object(
        pd.read_csv(ARCHIVO_INTERACCIONES, dtype=str, encoding="utf-8", keep_default_na=False)
    )
    bitacora = {
        "fecha_ejecucion_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "lectura_inicial": {"usuarios": len(usuarios), "interacciones": len(interacciones)},
    }
    print("=== ETL FASE 2 — COMMUNITY360 ===")
    print(f"[ETAPA 0] Lectura: usuarios={len(usuarios)}, interacciones={len(interacciones)}")

    usuarios, interacciones = etapa_1_deduplicar(usuarios, interacciones, bitacora)
    usuarios, interacciones = etapa_2_vacios(usuarios, interacciones, bitacora)
    usuarios, interacciones = etapa_3_formatos(usuarios, interacciones, bitacora)
    usuarios, interacciones = etapa_4_validar(usuarios, interacciones, bitacora)
    validas, cuarentena = etapa_5_integridad(usuarios, interacciones, bitacora)
    maestros, ubicacion, metricas = etapa_6_privacidad(usuarios, bitacora)
    metricas_tiempo_real = calcular_metricas(validas)
    etapa_7_exportar(maestros, ubicacion, metricas, cuarentena, validas, metricas_tiempo_real, bitacora)
    etapa_8_carga(maestros, ubicacion, metricas, cuarentena, validas, metricas_tiempo_real, bitacora)
    (DIR_SALIDA / "bitacora_etl.json").write_text(
        json.dumps(bitacora, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    verificar(bitacora)
    print("=== ETL FINALIZADO CORRECTAMENTE ===")


if __name__ == "__main__":
    main()
