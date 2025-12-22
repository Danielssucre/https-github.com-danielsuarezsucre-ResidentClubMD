import sqlite3
import os

# Configuración de ruta de DB (misma lógica que app.py)
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
DB_PATH_LOCAL = "prisma_srs.db"
# Verifica si existe la carpeta de datos de Render para decidir la ruta
DB_PATH = DB_PATH_RENDER if os.path.exists(os.path.dirname(DB_PATH_RENDER)) else DB_PATH_LOCAL

def upgrade_database():
    print(f"🛠️ Iniciando actualización de estructura de base de datos en: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    try:
        # Intentamos añadir la columna. Si ya existe, dará error y lo capturamos.
        c.execute("ALTER TABLE questions ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
        print("✅ Columna 'created_at' añadida exitosamente a la tabla 'questions'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("⚠️ La columna 'created_at' ya existía. No se requieren cambios.")
        else:
            print(f"❌ Error inesperado: {e}")
            
    conn.commit()
    conn.close()
    print("🏁 Migración finalizada.")

if __name__ == "__main__":
    upgrade_database()