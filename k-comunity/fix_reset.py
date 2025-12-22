import sqlite3
import os

# Configuración de ruta de DB (misma lógica que app.py/matriz_core.py)
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
DB_PATH_LOCAL = "prisma_srs.db"
DB_PATH = DB_PATH_RENDER if os.path.exists(DB_PATH_RENDER) else DB_PATH_LOCAL

def reset_topics():
    print(f"🔧 Conectando a la base de datos en: {DB_PATH}")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        print("🔄 Ejecutando: UPDATE matrix_topics SET status = 'PENDIENTE'...")
        cursor.execute("UPDATE matrix_topics SET status = 'PENDIENTE'")
        
        conn.commit()
        conn.close()
        print("✅ Éxito: Todos los temas han sido reiniciados a 'PENDIENTE'.")
        
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    reset_topics()