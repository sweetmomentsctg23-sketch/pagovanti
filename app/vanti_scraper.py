import asyncio
import httpx

async def consultar_factura_vanti(empresa: str, referencia: str) -> dict:
    """
    Cliente en Render que redirige la consulta de Vanti hacia el puente local 
    en la PC del usuario a través del túnel de Pyngrok, evitando bloqueos WAF.
    """
    if not empresa or not referencia or str(empresa).strip() == "" or str(referencia).strip() == "":
        return {
            "success": False,
            "message": "La empresa y la referencia son obligatorias para realizar la consulta."
        }

    # URL pública de tu túnel Pyngrok apuntando a tu FastAPI local en la PC
    url_puente_local = "https://litmus-upfront-failing.ngrok-free.dev/ejecutar-scraper-local"
    
    payload = {
        "empresa": str(empresa).strip(),
        "referencia": str(referencia).strip()
    }
    
    try:
        # Hacemos la petición HTTP POST hacia tu PC con un timeout generoso de 45 segundos
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(url_puente_local, json=payload)
            
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "success": False,
                    "message": f"Error en el puente local (Código {response.status_code}): {response.text}"
                }
                
    except httpx.RequestError as e:
        return {
            "success": False,
            "message": f"No se pudo conectar con el puente local en tu PC. ¿Están FastAPI y ngrok encendidos? Detalle: {str(e)}"
        }

# Mantenemos compatibilidad si alguna otra parte del sistema llama a la función sincrónica
def _consultar_factura_vanti_sync(empresa: str, referencia: str) -> dict:
    url_puente_local = "https://litmus-upfront-failing.ngrok-free.dev/ejecutar-scraper-local"
    payload = {
        "empresa": str(empresa).strip(),
        "referencia": str(referencia).strip()
    }
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.post(url_puente_local, json=payload)
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "success": False,
                    "message": f"Error en el puente local (Código {response.status_code}): {response.text}"
                }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error de conexión con el puente local: {str(e)}"
        }
