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
            estado TEXT, -- 'esperando', 'pagado', 'no_pagado'
            fecha DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    columnas = {columna[1] for columna in cursor.execute("PRAGMA table_info(transacciones)")}
    if "ultima_actividad" not in columnas:
        cursor.execute("ALTER TABLE transacciones ADD COLUMN ultima_actividad DATETIME")
        cursor.execute("UPDATE transacciones SET ultima_actividad = fecha WHERE ultima_actividad IS NULL")
    
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

def actualizar_estado_transaccion(tx_id: int, nuevo_estado: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE transacciones SET estado = ? WHERE id = ?
    ''', (nuevo_estado, tx_id))
    conn.commit()
    conn.close()

def actualizar_actividad_transaccion(tx_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE transacciones SET ultima_actividad = CURRENT_TIMESTAMP WHERE id = ?
    ''', (tx_id,))
    conn.commit()
    conn.close()

def obtener_transaccion(tx_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, empresa, referencia, monto, ip, estado, fecha, ultima_actividad,
               datetime(ultima_actividad) >= datetime('now', '-15 seconds') AS en_linea
        FROM transacciones WHERE id = ?
    ''', (tx_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0], "empresa": row[1], "referencia": row[2], "monto": row[3],
            "ip": row[4], "estado": row[5], "fecha": row[6],
            "ultima_actividad": row[7], "en_linea": bool(row[8])
        }
    return None

def obtener_todas_transacciones():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, empresa, referencia, monto, ip, estado, fecha, ultima_actividad,
               datetime(ultima_actividad) >= datetime('now', '-15 seconds') AS en_linea
        FROM transacciones
        ORDER BY en_linea DESC, id DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "empresa": r[1], "referencia": r[2], "monto": r[3],
            "ip": r[4], "estado": r[5], "fecha": r[6],
            "ultima_actividad": r[7], "en_linea": bool(r[8])
        }
        for r in rows
    ]

def obtener_metricas():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(monto) FROM transacciones WHERE estado = 'pagado'")
    pagados, total_dinero = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*) FROM transacciones WHERE estado = 'no_pagado'")
    no_pagados = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM transacciones")
    total_consultas = cursor.fetchone()[0]

    conn.close()
    return {
        "total_consultas": total_consultas or 0,
        "pagados_exitosos": pagados or 0,
        "no_pagados": no_pagados or 0,
        "total_dinero": total_dinero or 0.0
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
    if row and row[0] == code:
        return True
    return False