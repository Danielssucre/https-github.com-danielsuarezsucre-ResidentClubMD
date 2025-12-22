import sqlite3
import os
import time

# Configuración de ruta de DB (misma lógica que app.py)
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
DB_PATH_LOCAL = "prisma_srs.db"
# Verifica si existe la carpeta de datos de Render para decidir la ruta
DB_PATH = DB_PATH_RENDER if os.path.exists(os.path.dirname(DB_PATH_RENDER)) else DB_PATH_LOCAL

def activar_modo_turbo():
    print(f"🚀 Intentando activar modo WAL (Concurrencia) en: {DB_PATH}...")
    try:
        # Timeout alto para esperar a que se desbloquee si está ocupada
        conn = sqlite3.connect(DB_PATH, timeout=10)
        c = conn.cursor()
        
        # 1. Activar WAL
        c.execute("PRAGMA journal_mode=WAL;")
        mode = c.fetchone()[0]
        print(f"✅ Modo actual establecido a: {mode.upper()}")
        
        # 2. Sincronización normal (balance entre velocidad y seguridad)
        c.execute("PRAGMA synchronous=NORMAL;")
        print("✅ Sincronización ajustada.")
        
        # 3. Arreglar la columna created_at manualmente (ya que quitamos el default en el código)
        # Esto llena los huecos vacíos con la fecha actual
        print("🩹 Reparando fechas nulas...")
        c.execute("UPDATE questions SET created_at = DATETIME('now') WHERE created_at IS NULL")
        conn.commit()
        
        conn.close()
        print("🏁 Sistema optimizado y reparado.")
        
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    activar_modo_turbo()