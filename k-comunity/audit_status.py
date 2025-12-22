import sqlite3
import os
import time

# Configuración de ruta de DB (misma lógica que app.py)
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
DB_PATH_LOCAL = "prisma_srs.db"
# Verifica si existe la carpeta de datos de Render para decidir la ruta
DB_PATH = DB_PATH_RENDER if os.path.exists(os.path.dirname(DB_PATH_RENDER)) else DB_PATH_LOCAL

def audit_system():
    print(f"🕵️‍♂️ Iniciando Auditoría de Sistema en: {DB_PATH}")
    
    # 1. Existencia del archivo DB
    if os.path.exists(DB_PATH):
        print("✅ Archivo de base de datos encontrado.")
    else:
        print("❌ CRÍTICO: No se encuentra el archivo de base de datos.")
        return

    # 2. Búsqueda de archivos de bloqueo (Journal/WAL)
    journal_path = DB_PATH + "-journal"
    wal_path = DB_PATH + "-wal"
    
    locks_found = []
    if os.path.exists(journal_path):
        locks_found.append(f"JOURNAL ({journal_path})")
    if os.path.exists(wal_path):
        locks_found.append(f"WAL ({wal_path})")
        
    if locks_found:
        print(f"⚠️ ALERTA: Archivos temporales/bloqueo detectados: {', '.join(locks_found)}")
        print("   Esto indica una transacción abierta, un crash previo o modo WAL activo.")
    else:
        print("✅ No se detectaron archivos de bloqueo externos (-journal/-wal).")

    # 3. Prueba de Conexión (Timeout agresivo)
    print("Testing conexión (timeout=1s)...")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=1)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        print("✅ Conexión de lectura exitosa (DB no está totalmente bloqueada).")
        
        # 4. Chequeo de Journal Mode
        cursor.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        print(f"📊 Modo de Journal actual: '{mode.upper()}'")
        
        if mode.upper() in ['DELETE', 'TRUNCATE', 'PERSIST']:
            print("   ℹ️ Nota: Modos tradicionales (DELETE/TRUNCATE) son más propensos a 'database is locked' en concurrencia alta.")
        elif mode.upper() == 'WAL':
            print("   ℹ️ Nota: Modo WAL (Write-Ahead Logging) es mejor para concurrencia.")
            
        conn.close()
        
    except sqlite3.OperationalError as e:
        print(f"❌ FALLO DE CONEXIÓN: {e}")
        if "locked" in str(e).lower():
            print("   CONFIRMADO: La base de datos está bloqueada por otro proceso.")

if __name__ == "__main__":
    audit_system()