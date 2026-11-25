"""Catálogos y cálculo único de indicadores para Community360.

Lo usan tanto el ETL (datos.py) como la API (main.py), para que el documento
metricas_tiempo_real/resumen_global tenga siempre los mismos campos y fórmulas.
"""
import math
from datetime import datetime, timezone

CANALES_VALIDOS = ["Telegram", "Facebook", "Instagram", "TikTok", "Twitter/X", "Discord"]
TIPOS_VALIDOS = ["mensaje", "comentario", "reaccion", "respuesta_encuesta", "rsvp_evento"]
TEMAS_VALIDOS = [
    "arte_comisiones", "reglas_moderacion", "eventos_convenciones", "videojuegos",
    "memes_humor", "fursuits", "seguridad_privacidad", "musica",
]
DISPOSITIVOS_VALIDOS = ["Android", "iOS", "Web", "Escritorio"]
DIAS_HEATMAP = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
BLOQUES_HEATMAP = ["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"]

FORMULAS = {
    "total_interacciones": "N = número de interacciones vigentes",
    "sentimiento_promedio": "S̄ = (1/N) * Σ s_i",
    "total_reacciones": "R = Σ r_i",
    "reacciones_promedio": "R̄ = (1/N) * Σ r_i",
    "longitud_media_texto": "L̄ = (1/N_txt) * Σ len(texto_i), excluyendo reacciones",
    "por_canal / por_tema / por_tipo": "n_k = número de interacciones con valor k",
    "heatmap_actividad": "n(d,b) = interacciones del día d en el bloque de 4 h b (UTC)",
}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _numero(valor, tipo, defecto=0):
    try:
        numero = tipo(valor)
    except (TypeError, ValueError):
        return defecto
    if isinstance(numero, float) and not math.isfinite(numero):
        return defecto
    return numero


def sanitizar_json(valor):
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, dict):
        return {clave: sanitizar_json(item) for clave, item in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [sanitizar_json(item) for item in valor]
    return valor


def calcular_metricas(interacciones):
    """Calcula los indicadores globales a partir de una lista de diccionarios."""
    interacciones = list(interacciones)
    n = len(interacciones)
    sentimientos = [_numero(i.get("sentimiento"), float, 0.0) for i in interacciones]
    reacciones = [_numero(i.get("reacciones_recibidas"), int, 0) for i in interacciones]
    textos = [i for i in interacciones if i.get("tipo") != "reaccion"]
    longitudes = [len(i.get("texto") or "") for i in textos]

    por_canal = {c: 0 for c in CANALES_VALIDOS}
    por_tema = {t: 0 for t in TEMAS_VALIDOS}
    por_tipo = {t: 0 for t in TIPOS_VALIDOS}
    heatmap = {dia: {bloque: 0 for bloque in BLOQUES_HEATMAP} for dia in DIAS_HEATMAP}

    for i in interacciones:
        canal = i.get("canal") or "Sin canal"
        tema = i.get("tema") or "Sin tema"
        tipo = i.get("tipo") or "Sin tipo"
        por_canal[canal] = por_canal.get(canal, 0) + 1
        por_tema[tema] = por_tema.get(tema, 0) + 1
        por_tipo[tipo] = por_tipo.get(tipo, 0) + 1

        ts = i.get("timestamp")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
                heatmap[DIAS_HEATMAP[dt.weekday()]][BLOQUES_HEATMAP[min(dt.hour // 4, 5)]] += 1
            except ValueError:
                pass

    ultimas = sorted(interacciones, key=lambda x: str(x.get("timestamp", "")), reverse=True)[:10]
    campos_ultimas = [
        "id_interaccion", "id_usuario", "timestamp", "canal", "tipo", "tema",
        "sentimiento", "reacciones_recibidas", "texto",
    ]

    metricas = {
        "total_interacciones": n,
        "sentimiento_promedio": round(sum(sentimientos) / n, 6) if n else 0.0,
        "total_reacciones": sum(reacciones),
        "reacciones_promedio": round(sum(reacciones) / n, 6) if n else 0.0,
        "longitud_media_texto": round(sum(longitudes) / len(textos), 6) if textos else 0.0,
        "total_textos": len(textos),
        "por_canal": por_canal,
        "por_tema": por_tema,
        "por_tipo": por_tipo,
        "heatmap_actividad": heatmap,
        "ultimas_interacciones": [{c: i.get(c) for c in campos_ultimas} for i in ultimas],
        "ultima_actualizacion": utc_now_iso(),
        "formulas": FORMULAS,
    }
    return sanitizar_json(metricas)
