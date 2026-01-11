import sqlite3
import os
import shutil
import pandas as pd
import streamlit as st

# --- CONFIGURACIÓN DE RUTAS Y CONEXIÓN ---
# Configuración Estricta para Render
IS_RENDER = os.environ.get('RENDER', False)

# Ruta de la Base de Datos
# CORRECCIÓN: Render monta discos persistentes en /opt/render/data/
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
DB_PATH_LOCAL = "prisma_srs.db"
SEED_DB_PATH = os.path.join(os.path.dirname(__file__), "seed_database.db")

# Lógica de Selección de Ruta
if IS_RENDER:
    if os.path.exists(os.path.dirname(DB_PATH_RENDER)):
         DB_PATH = DB_PATH_RENDER
         print(f"✅ RENDER: Usando disco persistente en {DB_PATH}")
    else:
         # Fallback silencioso si no existe el directorio
         print(f"⚠️ ADVERTENCIA RENDER: No se encuentra {DB_PATH_RENDER}. Usando path local temporal.")
         DB_PATH = DB_PATH_LOCAL
else:
    DB_PATH = DB_PATH_LOCAL

# --- MIGRACIÓN AUTOMÁTICA DE SEED DATABASE ---
def _check_and_seed_database():
    """
    Si la BD de producción está vacía o no existe, copia el seed_database.db bundleado.
    Esto es una migración ONE-TIME para restaurar datos de backup.
    """
    if not os.path.exists(SEED_DB_PATH):
        print("ℹ️ No hay seed_database.db para migrar.")
        return
    
    need_seed = False
    
    if not os.path.exists(DB_PATH):
        need_seed = True
        print(f"📦 BD no existe en {DB_PATH}. Se usará seed.")
    else:
        # Verificar si la BD está vacía (0 preguntas)
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
            conn.close()
            if count == 0:
                need_seed = True
                print(f"📦 BD vacía detectada ({count} preguntas). Se usará seed.")
            else:
                print(f"✅ BD tiene {count} preguntas. No se necesita seed.")
        except Exception as e:
            need_seed = True
            print(f"⚠️ Error leyendo BD: {e}. Se usará seed.")
    
    if need_seed:
        try:
            shutil.copy2(SEED_DB_PATH, DB_PATH)
            print(f"✅ SEED MIGRATION: Copiado seed_database.db -> {DB_PATH}")
        except Exception as e:
            print(f"❌ Error copiando seed: {e}")

# Ejecutar migración al importar el módulo
_check_and_seed_database()

# Flag para evitar migraciones duplicadas
_TEMAS_MIGRATED = False

def _migrate_temas_table():
    """
    Migración para Carpetas por Tema:
    1. Crea tabla 'temas' si no existe
    2. Añade columna 'tema_id' a 'questions' si no existe
    3. Migra datos existentes de matrix_topics a temas
    """
    global _TEMAS_MIGRATED
    if _TEMAS_MIGRATED:
        return
    
    conn = None
    try:
        # Usar conexión con WAL mode para mejor concurrencia
        conn = sqlite3.connect(DB_PATH, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        
        # 1. Crear tabla temas
        conn.execute("""
            CREATE TABLE IF NOT EXISTS temas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT UNIQUE NOT NULL,
                categoria TEXT NOT NULL,
                total_preguntas INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at DATETIME
            )
        """)
        conn.commit()  # Commit inmediato para liberar lock
        
        # 2. Añadir tema_id a questions si no existe
        cols = [row[1] for row in conn.execute("PRAGMA table_info(questions)").fetchall()]
        if 'tema_id' not in cols:
            conn.execute("ALTER TABLE questions ADD COLUMN tema_id INTEGER REFERENCES temas(id)")
            conn.commit()
            print("✅ MIGRATION: Added tema_id column to questions")
        
        # 3. Migrar matrix_topics existentes a temas (si hay datos)
        try:
            existing_count = conn.execute("SELECT COUNT(*) FROM temas").fetchone()[0]
            if existing_count == 0:
                conn.execute("""
                    INSERT OR IGNORE INTO temas (nombre, categoria, created_at)
                    SELECT UPPER(topic_name), target_category, created_at 
                    FROM matrix_topics 
                    WHERE status = 'COMPLETADO' AND target_category IS NOT NULL
                """)
                conn.commit()
                migrated = conn.execute("SELECT COUNT(*) FROM temas").fetchone()[0]
                if migrated > 0:
                    print(f"✅ MIGRATION: Migrated {migrated} topics from matrix_topics to temas")
        except Exception:
            pass  # matrix_topics might not exist yet
        
        # 4. Vincular preguntas existentes a temas via tag_tema
        conn.execute("""
            UPDATE questions 
            SET tema_id = (SELECT id FROM temas WHERE UPPER(nombre) = UPPER(questions.tag_tema))
            WHERE tema_id IS NULL AND tag_tema IS NOT NULL
        """)
        conn.commit()
        
        _TEMAS_MIGRATED = True
        print("✅ TEMAS MIGRATION: Complete")
    except Exception as e:
        print(f"⚠️ TEMAS MIGRATION Warning: {e}")
    finally:
        if conn:
            conn.close()

# Ejecutar migración de temas
_migrate_temas_table()

def get_db_conn():
    """
    Establece conexión a SQLite con configuración optimizada para concurrencia (WAL).
    """
    # Timeout=30 obligatorio para evitar 'database is locked'
    conn = sqlite3.connect(DB_PATH, timeout=30) 
    conn.row_factory = sqlite3.Row
    
    # Optimizaciones de Rendimiento y Concurrencia
    try:
        # WAL Mode: Permite lecturas y escrituras simultáneas (CRÍTICO)
        conn.execute("PRAGMA journal_mode=WAL;") 
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception as e:
        print(f"⚠️ Advertencia DB: No se pudieron aplicar optimizaciones PRAGMA: {e}")
        
    return conn

def run_atomic_query(query, params=()):
    """
    Ejecuta una consulta de escritura (INSERT/UPDATE/DELETE) de forma atómica:
    Abre conexión -> Ejecuta -> Commit -> Cierra.
    Previene errores de 'Database is locked' o 'Closed database'.
    """
    conn = get_db_conn()
    try:
        conn.execute(query, params)
        conn.commit()
        return True
    except Exception as e:
        print(f"❌ Error DB Atomico: {e}")
        return False
    finally:
        conn.close()

def get_all_categories():
    conn = get_db_conn()
    try:
        rows = conn.execute("SELECT name FROM medical_categories ORDER BY name ASC").fetchall()
        return [row['name'] for row in rows]
    except Exception:
        return []
    finally:
        conn.close()

# --- LÓGICA DE NEGOCIO: GUEST MODE INTELLIGENT ---
def get_questions_for_guest(user_status='Aspirante', limit=10):
    conn = get_db_conn()
    questions = []
    try:
        if user_status == 'Ganador':
            # Ganador: Prioridad complejidad (Cardiología / Infectología)
            target_categories = ['Cardiología', 'Infectología', 'Neurología']
            placeholders = ','.join(['?'] * len(target_categories))
            query = f"SELECT * FROM questions WHERE status='active' AND tag_categoria IN ({placeholders}) ORDER BY RANDOM() LIMIT ?"
            params = target_categories + [limit]
            questions = conn.execute(query, params).fetchall()
            
            if len(questions) < limit:
                rem = limit - len(questions)
                extra = conn.execute("SELECT * FROM questions WHERE status='active' ORDER BY RANDOM() LIMIT ?", (rem,)).fetchall()
                questions.extend(extra)
        else: 
            # Aspirante: Medicina Interna (Diagnóstico) y Cirugía General
            # Normalización de Tags es crítica aquí
            target_categories = ['Medicina Interna', 'Cirugía General', 'Urgencias']
            placeholders = ','.join(['?'] * len(target_categories))
            query = f"SELECT * FROM questions WHERE status='active' AND tag_categoria IN ({placeholders}) ORDER BY RANDOM() LIMIT ?"
            params = target_categories + [limit]
            questions = conn.execute(query, params).fetchall()
            
            if len(questions) < limit:
                 # Fallback general
                 rem = limit - len(questions)
                 extra = conn.execute("SELECT * FROM questions WHERE status='active' ORDER BY RANDOM() LIMIT ?", (rem,)).fetchall()
                 questions.extend(extra)
    except Exception as e:
        print(f"❌ Error obteniendo preguntas guest: {e}")
    finally:
        conn.close()
    return [dict(q) for q in questions]

def setup_database():
    # Stub para mantener compatibilidad si se llama desde app.py, 
    # aunque idealmente app.py lo maneja o lo importamos todo aquí. 
    # Por ahora dejaremos que app.py maneje el setup masivo para no romper la lógica de migración existente alli.
    pass
