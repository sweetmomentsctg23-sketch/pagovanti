import asyncio
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from string import Template

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from pyngrok import ngrok
from playwright.sync_api import sync_playwright

app = FastAPI()

scraper_lock = asyncio.Lock()

class DatosConsulta(BaseModel):
    empresa: str
    referencia: str

def _consultar_factura_vanti_sync(empresa: str, referencia: str) -> dict:
    if not empresa or not referencia or str(empresa).strip() == "" or str(referencia).strip() == "":
        return {
            "success": False,
            "message": "La empresa y la referencia son obligatorias para realizar la consulta."
        }

    user_data_dir = os.path.abspath("./perfil_vanti_bots")

    lock_file = os.path.join(user_data_dir, "SingletonLock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass

    context = None
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel="chrome",
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1280,720",
                    "--disable-blink-features=AutomationControlled",
                    "--hide-crash-restore-bubble",
                    "--disable-infobars",
                    "--disable-features=Translate"  # Desactiva la barra de traducción
                ],
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
                java_script_enabled=True
            )

            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)

            context.add_cookies([
                {"name": "cookieconsent_status", "value": "dismiss", "domain": ".grupovanti.com", "path": "/"},
                {"name": "cb-enabled", "value": "accepted", "domain": ".grupovanti.com", "path": "/"}
            ])

            page = context.pages[0] if context.pages else context.new_page()

            print("🌐 1. Entrando a la pasarela de pagos de Vanti...")
            page.goto("https://pagosenlinea.grupovanti.com/", wait_until="domcontentloaded", timeout=30000)

            # ------------------------------------------------------------------
            # PASO 2: Seleccionar Empresa
            # ------------------------------------------------------------------
            print(f"🏢 2. Seleccionando empresa: {empresa}...")
            select_elem = page.locator('select#empresa')
            select_elem.wait_for(state="visible", timeout=15000)

            val_str = str(empresa).strip()
            try:
                select_elem.select_option(value=val_str, timeout=3000)
            except Exception:
                options = select_elem.locator('option').all()
                selected = False
                for opt in options:
                    opt_val = opt.get_attribute("value")
                    opt_text = opt.inner_text()
                    if opt_val == val_str or val_str.lower() in opt_text.lower():
                        select_elem.select_option(value=opt_val)
                        selected = True
                        break
                if not selected:
                    raise Exception(f"No se encontró la empresa: '{val_str}'")

            # ------------------------------------------------------------------
            # PASO 3: Ingresar Referencia
            # ------------------------------------------------------------------
            print(f"📝 3. Ingresando referencia: {referencia}...")
            input_elem = page.locator('input[formcontrolname="reference"], input[name="reference"]').first
            input_elem.wait_for(state="visible", timeout=5000)
            input_elem.click()
            input_elem.fill("")
            input_elem.press_sequentially(str(referencia), delay=30)
            
            input_elem.dispatch_event("input")
            input_elem.dispatch_event("change")
            input_elem.dispatch_event("blur")

            # ------------------------------------------------------------------
            # PASO 4: Seleccionar Método de Pago
            # ------------------------------------------------------------------
            print("🏦 4. Seleccionando método de pago...")
            label_bancolombia = page.locator('label[for="image2"], img[src*="bancolombia"]').first
            label_bancolombia.wait_for(state="visible", timeout=5000)
            label_bancolombia.scroll_into_view_if_needed()
            label_bancolombia.click()
            page.wait_for_timeout(1000)

            # Limpiar modales previos si existieran
            page.evaluate("""
                const overlays = document.querySelectorAll('.swal2-container, .modal-backdrop');
                overlays.forEach(el => el.remove());
            """)

            # ------------------------------------------------------------------
            # PASO 5: Clic en Consultar
            # ------------------------------------------------------------------
            print("🔍 5. Ejecutando botón de consulta...")
            btn = page.locator('button.query-button').first
            btn.wait_for(state="visible", timeout=5000)
            btn.click(force=True)

            # ------------------------------------------------------------------
            # PASO 6: Esperar y Extraer Respuesta (Flujo del código original)
            # ------------------------------------------------------------------
            print("⌛ 6. Esperando la respuesta en pantalla...")
            
            # Esperar a que el cargando (overlay) desaparezca
            overlay_selector = 'ngx-spinner, .ngx-spinner-overlay, block-ui-spinner, .block-ui-wrapper, div:has-text("Cargando...")'
            try:
                page.wait_for_selector(overlay_selector, state="visible", timeout=1500)
                page.wait_for_selector(overlay_selector, state="hidden", timeout=12000)
            except Exception:
                pass

            # Esperar que aparezca el selector del resultado (monto o alerta SweetAlert)
            selector_resultado = 'label.disabled, #swal2-html-container, .swal2-popup'
            page.wait_for_selector(selector_resultado, state="visible", timeout=25000)

            # A) Evaluar si es un mensaje de Error o Estado (SweetAlert2)
            swal_text = page.locator('#swal2-html-container').first
            if swal_text.count() > 0 and swal_text.is_visible():
                mensaje_error = swal_text.inner_text().strip()
                if mensaje_error:
                    print(f"⚠️ Alerta recibida: {mensaje_error}")
                    return {
                        "success": False,
                        "message": mensaje_error
                    }

            # B) Evaluar y extraer el valor a pagar
            monto = 0.0
            texto_monto = ""
            labels_disabled = page.locator('label.disabled').all()

            for lbl in labels_disabled:
                txt = lbl.inner_text().strip()
                if "$" in txt:
                    texto_monto = txt
                    break

            if texto_monto:
                texto_limpio = unicodedata.normalize("NFKC", texto_monto)
                texto_limpio = texto_limpio.replace(str(referencia), "")
                texto_limpio = texto_limpio.replace(" ", "").strip()
                
                match = re.search(r'\$?([\d\.\,]+)', texto_limpio)
                if match:
                    val_str = match.group(1)
                    if "," in val_str and "." in val_str:
                        val_str = val_str.replace(',', '')
                    elif "," in val_str:
                        partes = val_str.split(",")
                        if len(partes[-1]) == 3:
                            val_str = val_str.replace(',', '')
                        else:
                            val_str = val_str.replace(',', '.')
                    elif "." in val_str:
                        partes = val_str.split(".")
                        if len(partes[-1]) == 3:
                            val_str = val_str.replace('.', '')

                    try:
                        monto = float(val_str)
                    except ValueError:
                        pass

            if monto > 0:
                print(f"✅ ¡Éxito! Monto encontrado: {monto}")
                return {
                    "success": True,
                    "reference": referencia,
                    "amount": monto,
                    "name": "Usuario Vanti"
                }
            else:
                return {
                    "success": False,
                    "message": "No se pudo extraer el valor a pagar de la pantalla.",
                    "raw_text": texto_monto
                }

    except Exception as e:
        print(f"❌ Error en automatización: {str(e)}")
        return {
            "success": False,
            "message": f"Error en la automatización local: {str(e)}"
        }

    finally:
        # Cierre garantizado del navegador inmediatamente al terminar
        if context:
            try:
                context.close()
                print("🔒 Navegador cerrado. Listo para la siguiente consulta.")
            except Exception:
                pass

@app.post("/ejecutar-scraper-local")
async def recibir_peticion(datos: DatosConsulta):
    print(f"\n-> Petición recibida para empresa: {datos.empresa}, ref: {datos.referencia}")
    async with scraper_lock:
        resultado = await asyncio.to_thread(_consultar_factura_vanti_sync, datos.empresa, datos.referencia)
    print(f"<- Resultado obtenido: {resultado}")
    return resultado

# ==================================================================
#  ePayco: URL de Respuesta y URL de Confirmación
# ==================================================================
# Configura tus credenciales con variables de entorno antes de ejecutar:
#   export EPAYCO_P_CUST_ID="tu_p_cust_id"
#   export EPAYCO_P_KEY="tu_p_key"
#   export EPAYCO_TEST="true"   # (opcional) modo pruebas
EPAYCO_P_CUST_ID = "1594891"
EPAYCO_P_KEY =  "e934b987624597d78a2ad09aaf880a8babce2636"
EPAYCO_TEST = os.getenv("EPAYCO_TEST", "false").lower() in ("1", "true", "si", "yes")

# ------------------------------------------------------------------
# 👉 COLOCA AQUÍ TU URL: todos los eventos de ePayco (respuesta y
#    confirmación) se reenviarán a esta dirección vía HTTP POST (JSON).
#    Ejemplo: EPAYCO_URL_DESTINO = "https://tu-servidor.com/webhook-epayco"
#    Déjala vacía ("") si no quieres reenviar los eventos.
# ------------------------------------------------------------------
EPAYCO_URL_DESTINO = os.getenv("EPAYCO_URL_DESTINO", "")

EPAYCO_VALIDATION_URL = (
    "https://secure.epayco.io/validation/v1/reference/{}"
    if EPAYCO_TEST
    else "https://secure.epayco.co/validation/v1/reference/{}"
)

EPAYCO_LOG_FILE = os.path.abspath("./epayco_notificaciones.jsonl")

# x_cod_response -> (texto, estado local en la BD)
EPAYCO_ESTADOS = {
    "1": ("Aceptada", "pagado"),
    "2": ("Rechazada", "no_pagado"),
    "3": ("Pendiente", "esperando"),
    "4": ("Fallida", "no_pagado"),
}


def _firma_epayco_valida(data: dict) -> bool:
    """Valida la firma x_signature enviada por ePayco (checkout estándar)."""
    if not EPAYCO_P_CUST_ID or not EPAYCO_P_KEY:
        print("⚠️ EPAYCO_P_CUST_ID / EPAYCO_P_KEY no configurados: se omite validación de firma.")
        return True
    cadena = (
        f"{EPAYCO_P_CUST_ID}^{EPAYCO_P_KEY}^{data.get('x_ref_payco', '')}^"
        f"{data.get('x_transaction_id', '')}^{data.get('x_amount', '')}^"
        f"{data.get('x_currency_code', '')}"
    )
    esperada = hashlib.sha256(cadena.encode("utf-8")).hexdigest()
    recibida = str(data.get("x_signature", "")).strip().lower()
    return esperada == recibida


def _registrar_notificacion_epayco(data: dict, origen: str):
    """Guarda cada notificación en un archivo JSONL para trazabilidad."""
    try:
        registro = {
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "origen": origen,
            "datos": data,
        }
        with open(EPAYCO_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ No se pudo guardar el log de ePayco: {e}")


async def _reenviar_evento_epayco(data: dict, origen: str):
    """Reenvía el evento a EPAYCO_URL_DESTINO (si está configurada)."""
    if not EPAYCO_URL_DESTINO:
        return
    try:
        payload = {
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "origen": origen,
            "datos": data,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(EPAYCO_URL_DESTINO, json=payload)
        print(f"📤 Evento '{origen}' reenviado a {EPAYCO_URL_DESTINO} -> HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ No se pudo reenviar el evento a {EPAYCO_URL_DESTINO}: {e}")


def _actualizar_transaccion_local(referencia: str, estado: str):
    """Marca la transacción local como pagada/no_pagada según la confirmación."""
    try:
        import sqlite3
        if not os.path.exists("vanti_data.db"):
            return
        refs = {str(referencia)}
        if "-" in str(referencia):  # soporta facturas tipo '61743859-123'
            refs.add(str(referencia).split("-")[0])
        placeholders = ",".join("?" for _ in refs)
        conn = sqlite3.connect("vanti_data.db")
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE transacciones SET estado = ? WHERE referencia IN ({placeholders}) AND estado != 'pagado'",
            (estado, *refs),
        )
        if cursor.rowcount > 0:
            print(f"💾 BD actualizada: referencia {referencia} -> '{estado}'")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ No se pudo actualizar la base de datos local: {e}")


def _formatear_monto(valor: str, moneda: str) -> str:
    try:
        monto = float(valor)
        return f"$ {monto:,.0f} {moneda}".replace(",", ".")
    except (TypeError, ValueError):
        return f"{valor} {moneda}"


_RESPUESTA_HTML = Template("""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Resultado de tu pago</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }
    body { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #f0f4f8; padding: 20px; }
    .card { background: #fff; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,.1); max-width: 420px; width: 100%; padding: 40px 30px; text-align: center; }
    .icono { width: 80px; height: 80px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 40px; margin: 0 auto 20px; background: $color_fondo; color: $color; }
    h1 { font-size: 22px; color: #1a202c; margin-bottom: 8px; }
    .estado { font-size: 15px; color: $color; font-weight: 600; margin-bottom: 24px; }
    .detalle { text-align: left; border-top: 1px solid #e2e8f0; padding-top: 16px; }
    .fila { display: flex; justify-content: space-between; padding: 8px 0; font-size: 14px; }
    .fila span:first-child { color: #718096; }
    .fila span:last-child { color: #2d3748; font-weight: 600; text-align: right; }
    .pie { margin-top: 24px; font-size: 12px; color: #a0aec0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icono">$icono</div>
    <h1>$titulo</h1>
    <p class="estado">$estado</p>
    <div class="detalle">$filas</div>
    <p class="pie">Gracias por usar nuestro portal de pagos.</p>
  </div>
</body>
</html>""")


def _pagina_respuesta(datos: dict) -> str:
    cod = str(datos.get("x_cod_response", "")).strip()
    estado_txt = datos.get("x_response") or EPAYCO_ESTADOS.get(cod, ("Desconocido", ""))[0]

    if cod == "1":
        icono, color, fondo, titulo = "✓", "#16a34a", "#dcfce7", "¡Pago exitoso!"
    elif cod == "3":
        icono, color, fondo, titulo = "…", "#d97706", "#fef3c7", "Pago pendiente"
    else:
        icono, color, fondo, titulo = "✕", "#dc2626", "#fee2e2", "Pago no completado"

    campos = [
        ("Factura / Referencia", datos.get("x_id_invoice", "")),
        ("Ref. ePayco", datos.get("x_ref_payco", "")),
        ("Valor", _formatear_monto(datos.get("x_amount", ""), datos.get("x_currency_code", "COP"))),
        ("Fecha", datos.get("x_transaction_date", "")),
        ("Medio de pago", datos.get("x_franchise", "")),
        ("Autorización", datos.get("x_approval_code", "")),
        ("Detalle", datos.get("x_response_reason_text", "")),
    ]
    filas = "".join(
        f'<div class="fila"><span>{k}</span><span>{v}</span></div>'
        for k, v in campos if v not in (None, "")
    ) or '<div class="fila"><span>Info</span><span>No se recibieron datos de la transacción.</span></div>'

    return _RESPUESTA_HTML.substitute(
        icono=icono, color=color, color_fondo=fondo,
        titulo=titulo, estado=f"Estado: {estado_txt}", filas=filas,
    )


async def _extraer_datos_epayco(request: Request) -> dict:
    """ePayco puede enviar los datos por querystring, form-data o JSON."""
    data = dict(request.query_params)
    if request.method == "POST":
        try:
            if "application/json" in request.headers.get("content-type", ""):
                data.update(await request.json())
            else:
                data.update(dict(await request.form()))
        except Exception:
            pass
    return data


@app.api_route("/epayco/respuesta", methods=["GET", "POST"], response_class=HTMLResponse)
async def epayco_respuesta(request: Request):
    """URL de Respuesta: aquí regresa el cliente después de pagar."""
    params = await _extraer_datos_epayco(request)
    ref_payco = params.get("ref_payco", "")
    print(f"\n↩️  [ePayco] URL de Respuesta - ref_payco: {ref_payco}")

    datos = {}
    if ref_payco:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(EPAYCO_VALIDATION_URL.format(ref_payco))
                body = r.json()
                if body.get("success"):
                    datos = body.get("data", {})
        except Exception as e:
            print(f"⚠️ Error consultando validación de ePayco: {e}")

    _registrar_notificacion_epayco(datos or params, "respuesta")
    await _reenviar_evento_epayco(datos or params, "respuesta")
    return HTMLResponse(_pagina_respuesta(datos))


@app.api_route("/epayco/confirmacion", methods=["GET", "POST"])
async def epayco_confirmacion(request: Request):
    """URL de Confirmación: webhook servidor a servidor de ePayco."""
    data = await _extraer_datos_epayco(request)
    print(f"\n🔔 [ePayco] Confirmación recibida: {json.dumps(data, ensure_ascii=False)}")

    if not _firma_epayco_valida(data):
        print("⛔ Firma inválida. Se rechaza la notificación.")
        return JSONResponse({"success": False, "message": "Firma inválida"}, status_code=400)

    cod = str(data.get("x_cod_response", "")).strip()
    estado_txt, estado_local = EPAYCO_ESTADOS.get(cod, ("Desconocido", "esperando"))
    referencia = data.get("x_id_invoice") or data.get("x_extra1") or ""

    _registrar_notificacion_epayco(data, "confirmacion")
    await _reenviar_evento_epayco(data, "confirmacion")

    if referencia and cod in ("1", "2", "4"):
        _actualizar_transaccion_local(referencia, estado_local)

    print(
        f"✅ [ePayco] Confirmación procesada -> factura: {referencia} | "
        f"estado: {estado_txt} | monto: {data.get('x_amount')} {data.get('x_currency_code')} | "
        f"ref_payco: {data.get('x_ref_payco')}"
    )
    return {"success": True, "message": "Confirmación recibida", "estado": estado_txt}


if __name__ == "__main__":
    import uvicorn
    public_url = ngrok.connect(8000)
    print(f"\n🚀 TÚNEL ACTIVO:")
    print(f"   • Scraper:             {public_url}/ejecutar-scraper-local")
    print(f"   • ePayco Respuesta:    {public_url}/epayco/respuesta")
    print(f"   • ePayco Confirmación: {public_url}/epayco/confirmacion\n")
    print("   Configura estas dos últimas URLs en el panel de ePayco:")
    print("   (Integraciones -> Llaves API -> URLs de respuesta y confirmación)\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)