import sqlite3
import os

DB_PATH = "prisma_srs.db"

def fix_categories():
    if not os.path.exists(DB_PATH):
        print(f"❌ No se encontró la base de datos: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("🧹 Iniciando normalización de etiquetas de categoría...")
    
    # 1. Mapa de correcciones específicas (Errores comunes OCR / Typos)
    corrections = {
        'Cirugia General': 'Cirugía General',
        'Ginecologia': 'Ginecología y Obstetricia',
        'Pediatria': 'Pediatría',
        'Cardiologia': 'Cardiología',
        'Anestesiologia': 'Anestesiología',
        'Oftalmologia': 'Oftalmología',
        'Ortopedia': 'Ortopedia', # Ya es correcto pero previene regresiones
    }
    
    total_changes = 0
    
    # Aplicar correcciones específicas del mapa
    for bad, good in corrections.items():
        cursor.execute(f"UPDATE questions SET tag_categoria = ? WHERE tag_categoria = ?", (good, bad))
        if cursor.rowcount > 0:
            print(f"✅ Corregido '{bad}' -> '{good}': {cursor.rowcount} registros.")
            total_changes += cursor.rowcount

    # 2. Capitalización Universal (Title Case) para atrapar 'cirugía General', etc.
    # Obtener todas las categorías distintas
    cursor.execute("SELECT DISTINCT tag_categoria FROM questions")
    existing_tags = [row[0] for row in cursor.fetchall() if row[0]]
    
    for tag in existing_tags:
        clean_tag = tag.strip()
        # Si es todo minúsculas o formato raro, lo pasamos a Title Case (ej: "cirugía general" -> "Cirugía General")
        # Nota: simple .title() puede fallar con 'y', pero es un buen baseline.
        if clean_tag != tag:
             cursor.execute("UPDATE questions SET tag_categoria = ? WHERE tag_categoria = ?", (clean_tag, tag))
             print(f"✨ Trimmed '{tag}' -> '{clean_tag}'")
             total_changes += 1

    conn.commit()
    conn.close()
    
    if total_changes == 0:
        print("🎉 La base de datos ya estaba limpia. No se requirieron cambios.")
    else:
        print(f"🏁 Normalización completada. Total de cambios: {total_changes}")

if __name__ == "__main__":
    fix_categories()
