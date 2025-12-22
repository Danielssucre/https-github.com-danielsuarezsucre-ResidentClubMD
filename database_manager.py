import sqlite3
import os
import pandas as pd
import streamlit as st

# --- CONFIGURACIÓN DE RUTAS Y CONEXIÓN ---
# Detección de entorno Render
IS_RENDER = os.environ.get('RENDER', False)

# Ruta de la Base de Datos
DB_PATH_RENDER = "/var/data/prisma_srs.db"  # Disco persistente en Render
DB_PATH_LOCAL = "prisma_srs.db"
DB_PATH = DB_PATH_RENDER if IS_RENDER else DB_PATH_LOCAL

def get_db_conn():
    """
    Establece conexión a SQLite con configuración optimizada para concurrencia (WAL).
    """
    conn = sqlite3.connect(DB_PATH, timeout=30) # Timeout aumentado a 30s
    conn.row_factory = sqlite3.Row
    
    # Optimizaciones de Rendimiento y Concurrencia
    try:
        # WAL Mode: Permite lecturas y escrituras simultáneas
        conn.execute("PRAGMA journal_mode=WAL;") 
        # Synchronous NORMAL: Balance entre seguridad y velocidad
        conn.execute("PRAGMA synchronous=NORMAL;")
        # Foreign Keys: Integridad referencial
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception as e:
        print(f"⚠️ Advertencia DB: No se pudieron aplicar optimizaciones PRAGMA: {e}")
        
    return conn

def setup_database():
    """Migraciones y configuración inicial (Delegar a app.py o centralizar aquí si se desea refactorizar todo)"""
    # Por ahora mantenemos la lógica de creación en app.py para no romper el monolito abruptamente,
    # pero este manager manejará las conexiones.
    pass

# --- LÓGICA DE NEGOCIO: GUEST MODE ---

def get_questions_for_guest(user_status='Aspirante', limit=10):
    """
    Recupera preguntas para el Modo Invitado con lógica inteligente.
    - Ganador: Prioridad a temas complejos/diversos para minería.
    - Aspirante: Prioridad a Medicina Interna y Cirugía General (Diagnóstico).
    """
    conn = get_db_conn()
    questions = []
    
    try:
        if user_status == 'Ganador':
            # ESTRATEGIA: "Desafío de Experto"
            # Busca preguntas de alta dificultad (si existiera métrica, simulamos con random de todo el banco)
            # O temas menos comunes para ampliar la cobertura de minería.
            query = """
                SELECT * FROM questions 
                WHERE status = 'active' 
                ORDER BY RANDOM() 
                LIMIT ?
            """
            questions = conn.execute(query, (limit,)).fetchall()
            
        else: # Aspirante (Default)
            # ESTRATEGIA: "Diagnóstico de Nivel"
            # Foco en Medicina Interna (que incluye Cardio, Neumo, etc. normalizados) y Cirugía General
            # Usamos los tags normalizados.
            target_categories = ['Medicina Interna', 'Cirugía General', 'Cardiología', 'Neumología', 'Gastroenterología']
            placeholders = ','.join(['?'] * len(target_categories))
            
            query = f"""
                SELECT * FROM questions 
                WHERE status = 'active'
                AND tag_categoria IN ({placeholders})
                ORDER BY RANDOM()
                LIMIT ?
            """
            params = target_categories + [limit]
            questions = conn.execute(query, params).fetchall()
            
            # Fallback: Si no hay suficientes preguntas de esas categorías, rellenar con cualquiera
            if len(questions) < limit:
                remaining = limit - len(questions)
                fallback_query = "SELECT * FROM questions WHERE status = 'active' ORDER BY RANDOM() LIMIT ?"
                extra_questions = conn.execute(fallback_query, (remaining,)).fetchall()
                questions.extend(extra_questions)

    except Exception as e:
        print(f"❌ Error obteniendo preguntas guest: {e}")
        return []
    finally:
        conn.close()
        
    # Convertir a lista de diccionarios para facilitar uso en UI
    return [dict(q) for q in questions]

# --- UTILIDADES GENERALES ---

def get_all_categories():
    conn = get_db_conn()
    try:
        rows = conn.execute("SELECT name FROM medical_categories ORDER BY name ASC").fetchall()
        return [row['name'] for row in rows]
    except Exception:
        return []
    finally:
        conn.close()
