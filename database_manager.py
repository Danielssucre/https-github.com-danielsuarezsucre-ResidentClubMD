import sqlite3
import os
import pandas as pd
import streamlit as st

# --- CONFIGURACIÓN DE RUTAS Y CONEXIÓN ---
# Configuración Estricta para Render
IS_RENDER = os.environ.get('RENDER', False)

# Ruta de la Base de Datos
# CORRECCIÓN: Render monta discos persistentes en /opt/render/data/
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
DB_PATH_LOCAL = "prisma_srs.db"

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
