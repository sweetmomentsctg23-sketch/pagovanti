import sqlite3
import os

DB_PATH = "vanti_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Tabla de transacciones
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transacciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa TEXT,
            referencia TEXT,
            monto REAL,
            ip TEXT,
            estado TEXT, -- 'esperando', 'pagado', 'no_pagado', 'por_verificar'
            fecha DATETIME DEFAULT CURRENT_TIMESTAMP,
            ultima_actividad DATETIME,
            banco TEXT,
            franquicia TEXT,
            metodo_pago TEXT,
            ref_payco TEXT
        )
    ''')
    
    columnas = {columna[1] for columna in cursor.execute("PRAGMA table_info(transacciones)")}
    
    if "ultima_actividad" not in columnas:
        cursor.execute("ALTER TABLE transacciones ADD COLUMN ultima_actividad DATETIME")
        cursor.execute("UPDATE transacciones SET ultima_actividad = fecha WHERE ultima_actividad IS NULL")
    if "banco" not in columnas:
        cursor.execute("ALTER TABLE transacciones ADD COLUMN banco TEXT")
    if "franquicia" not in columnas:
        cursor.execute("ALTER TABLE transacciones ADD COLUMN franquicia TEXT")
    if "metodo_pago" not in columnas:
        cursor.execute("ALTER TABLE transacciones ADD COLUMN metodo_pago TEXT")
    if "ref_payco" not in columnas:
        cursor.execute("ALTER TABLE transacciones ADD COLUMN ref_payco TEXT")

    # Tabla de IPs bloqueadas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ips_bloqueadas (
            ip TEXT PRIMARY KEY
        )
    ''')

    # Tabla para OTP Telegram de Admin
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_otp (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            code TEXT,
            creado_en DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()

def guardar_transaccion(empresa: str, referencia: str, monto: float, ip: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO transacciones (empresa, referencia, monto, ip, estado, ultima_actividad)
        VALUES (?, ?, ?, ?, 'esperando', CURRENT_TIMESTAMP)
    ''', (empresa, referencia, monto, ip))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return tx_id

def actualizar_detalle_epayco(tx_id: int, estado: str, banco: str = None, franquicia: str = None, metodo: str = None, ref_payco: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE transacciones 
        SET estado = ?, banco = ?, franquicia = ?, metodo_pago = ?, ref_payco = ?
        WHERE id = ?
    ''', (estado, banco, franquicia, metodo, ref_payco, tx_id))
    conn.commit()
    conn.close()

def actualizar_estado_transaccion(tx_id: int, nuevo_estado: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE transacciones SET estado = ? WHERE id = ?', (nuevo_estado, tx_id))
    conn.commit()
    conn.close()

def actualizar_actividad_transaccion(tx_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE transacciones SET ultima_actividad = CURRENT_TIMESTAMP WHERE id = ?', (tx_id,))
    conn.commit()
    conn.close()

def obtener_transaccion(tx_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, empresa, referencia, monto, ip, estado, fecha, ultima_actividad,
               datetime(ultima_actividad) >= datetime('now', '-15 seconds') AS en_linea,
               banco, franquicia, metodo_pago, ref_payco
        FROM transacciones WHERE id = ?
    ''', (tx_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0], "empresa": row[1], "referencia": row[2], "monto": row[3],
            "ip": row[4], "estado": row[5], "fecha": row[6],
            "ultima_actividad": row[7], "en_linea": bool(row[8]),
            "banco": row[9], "franquicia": row[10], "metodo_pago": row[11], "ref_payco": row[12]
        }
    return None

def obtener_todas_transacciones():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, empresa, referencia, monto, ip, estado, fecha, ultima_actividad,
               datetime(ultima_actividad) >= datetime('now', '-15 seconds') AS en_linea,
               banco, franquicia, metodo_pago, ref_payco
        FROM transacciones
        ORDER BY en_linea DESC, id DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "empresa": r[1], "referencia": r[2], "monto": r[3],
            "ip": r[4], "estado": r[5], "fecha": r[6],
            "ultima_actividad": r[7], "en_linea": bool(r[8]),
            "banco": r[9], "franquicia": r[10], "metodo_pago": r[11], "ref_payco": r[12]
        }
        for r in rows
    ]

def obtener_metricas():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Total dinero cobrado / pagado
    cursor.execute("SELECT COUNT(*), SUM(monto) FROM transacciones WHERE estado IN ('pagado', 'Aceptada')")
    pagados, total_dinero = cursor.fetchone()
    
    # Total dinero rechazado / no pagado
    cursor.execute("SELECT COUNT(*), SUM(monto) FROM transacciones WHERE estado IN ('no_pagado', 'Rechazada', 'Fallida')")
    no_pagados, dinero_rechazado = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM transacciones")
    total_consultas = cursor.fetchone()[0]

    conn.close()
    return {
        "total_consultas": total_consultas or 0,
        "pagados_exitosos": pagados or 0,
        "no_pagados": no_pagados or 0,
        "total_dinero": total_dinero or 0.0,
        "dinero_rechazado": dinero_rechazado or 0.0
    }

def bloquear_ip(ip: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO ips_bloqueadas (ip) VALUES (?)', (ip,))
    conn.commit()
    conn.close()

def es_ip_bloqueada(ip: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT ip FROM ips_bloqueadas WHERE ip = ?', (ip,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def guardar_otp_admin(code: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO admin_otp (id, code) VALUES (1, ?)', (code,))
    conn.commit()
    conn.close()

def verificar_otp_admin(code: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT code FROM admin_otp WHERE id = 1')
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == code)
