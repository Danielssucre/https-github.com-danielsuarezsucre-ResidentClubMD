import math
import random
import datetime
import streamlit as st
import database_manager as dbm

# --- MOTOR FSRS v5 ---
class FSRS_v5_Engine:
    def __init__(self):
        # Parámetros estándar FSRS v5 (The 17 Weights)
        self.p = [0.40255, 1.18385, 3.173, 15.69105, 7.19605, 0.5345, 1.4604, 0.0046, 0.618, 2.4849, 0.0191, 0.9695, 0.2863, 0.9012, 1.3068, 0.7197, 2.8796]
        self.request_retention = 0.90

    def calculate_next_review(self, current_s, current_d, current_reps, rating, days_elapsed):
        # Rating: 1 (Again), 2 (Hard), 3 (Good), 4 (Easy)
        # Si es la primera vez (reps=0)
        if current_reps == 0:
            new_s = self.p[rating - 1]
            new_d = self.p[4] - (rating - 3) * self.p[5]
            new_d = min(max(new_d, 1), 10)
            return new_s, new_d
        # Repasos posteriores
        # 1. Actualizar Dificultad (D)
        new_d = current_d - self.p[6] * (rating - 3)
        new_d = self.p[5] * self.p[0] + (1 - self.p[5]) * new_d
        new_d = min(max(new_d, 1), 10)
        # 2. Actualizar Estabilidad (S)
        if rating == 1: # Olvido
            new_s = self.p[11] * math.pow(new_d, -self.p[12]) * (math.pow(current_s + 1, self.p[13]) - 1) * math.exp(self.p[14] * (1 - self.request_retention))
        else: # Recuerdo
            hard_penalty = self.p[15] if rating == 2 else 1
            easy_bonus = self.p[16] if rating == 4 else 1
            # Factor de ganancia de estabilidad
            s_inc = math.exp(self.p[8]) * (11 - new_d) * math.pow(current_s, -self.p[9]) * (math.exp(self.p[10] * (1 - self.request_retention)) - 1) * hard_penalty * easy_bonus
            new_s = current_s * (1 + s_inc)
        return new_s, new_d
        
    def get_next_interval_days(self, stability):
        new_interval = stability * 9 * (1 / self.request_retention - 1)
        return max(1, round(new_interval))

# DEFINICIÓN MAESTRA DEL GOLDEN RATIO (Sin tildes para compatibilidad)
GOLDEN_RATIO_DETAILED = {
    'Pediatría': 16,
    'Ginecología y Obstetricia': 14,
    'Cardiología': 12,
    'Cirugía General': 10,
    'Urgencias': 10,
    'Infectología': 8,
    'Nefrología': 8,
    'Neurología': 8,
    'Endocrinología': 7,
    'Neumología': 7,
    'Gastroenterología': 6,
    'Psiquiatría': 6,
    'Hematología': 5,
    'Reumatología': 5,
    'Ortopedia': 5,
    'Epidemiología': 5,
    'Urología': 4,
    'Otorrinolaringología': 3,
    'Oftalmología': 3,
    'Neurocirugía': 3,
    'Vascular': 3
}

def get_next_question_for_user(username, practice_mode=False, study_mode='AUTO'):
    """
    Obtiene la próxima pregunta según el Modo de Estudio. (Reingeniería Segmentada)
    
    study_mode:
      - 'REVIEW': Solo repaso (due_date <= hoy)
      - 'LEARN': Solo nuevas (sin progress)
      - 'AUTO': Flujo infinito original (mezcla inteligente)
      - 'MIXED': Modo controlado por volumen (nuevas primero, luego repasos por estabilidad)
      
    Devuelve un diccionario {'id': question_id, 'is_advance': bool, 'type': 'new'|'review'} o None.
    """
    conn = dbm.get_db_conn()
    cursor = conn.cursor()
    today = datetime.date.today().isoformat()

    try:
        # --- A. MODO PRÁCTICA (BIBLIOTECA) ---
        # Prioridad absoluta si el usuario eligió un tema específico
        if st.session_state.get('practice_mode'):
            practice_question = None
            # ... (Lógica de práctica existente se mantiene igual, omitiendo por brevedad en reemplazo si no se toca)
            # COPIAR LOGICA EXISTENTE DE PRÁCTICA AQUÍ (Simplificando el patch para no borrarla)
            # Como la herramienta replace reemplaza TODO el bloque, debo incluir la lógica de práctica.
            
            # Caso A: Tag Único - FIX: Usar LIKE para acentos
            if st.session_state.get('selected_tag'):
                tag = st.session_state.selected_tag
                cursor.execute("SELECT id FROM questions WHERE (tag_tema = ? OR tag_tema LIKE ?) AND status = 'active' ORDER BY RANDOM() LIMIT 1", (tag, f"%{tag}%"))
                practice_question = cursor.fetchone()
            
            # Caso A.1: CARPETA POR TEMA (tema_id específico)
            elif st.session_state.get('active_tema_id'):
                tema_id = st.session_state.active_tema_id
                answered_ids = st.session_state.get('session_answered_ids', [])
                
                params = [tema_id, username]
                exclude_clause = ""
                if answered_ids:
                    placeholders_exclude = ','.join(['?'] * len(answered_ids))
                    exclude_clause = f"AND q.id NOT IN ({placeholders_exclude})"
                    params.extend(answered_ids)
                
                # Buscar preguntas del tema que el usuario no haya respondido en esta sesión
                query = f"""
                    SELECT q.id FROM questions q
                    LEFT JOIN progress p ON q.id = p.question_id AND p.username = ?
                    WHERE q.tema_id = ? AND q.status = 'active'
                    {exclude_clause}
                    ORDER BY CASE WHEN p.question_id IS NULL THEN 0 ELSE 1 END, RANDOM()
                    LIMIT 1
                """
                # Reordenar params: username primero para JOIN, luego tema_id
                cursor.execute(query.replace("q.tema_id = ?", "q.tema_id = ?"), [username, tema_id] + (answered_ids if answered_ids else []))
                practice_question = cursor.fetchone()
                
            # Caso B: Especialidad
            elif st.session_state.get('practice_specialty'):
                specialty = st.session_state.practice_specialty
                answered_ids = st.session_state.get('session_answered_ids', [])
                
                # FIX: Buscar por coincidencia exacta O por prefijo flexible
                # SQLite LOWER() no maneja acentos, así que usamos LIKE con el valor original
                params = [f"%{specialty}%", specialty]
                exclude_clause = ""
                if answered_ids:
                    placeholders_exclude = ','.join(['?'] * len(answered_ids))
                    exclude_clause = f"AND id NOT IN ({placeholders_exclude})"
                    params.extend(answered_ids)
                
                # Buscar por coincidencia parcial O exacta
                query = f"""
                    SELECT id FROM questions 
                    WHERE (tag_categoria LIKE ? OR tag_categoria = ?) 
                    {exclude_clause} 
                    AND status = 'active' 
                    ORDER BY RANDOM() LIMIT 1
                """
                cursor.execute(query, params)
                practice_question = cursor.fetchone()

            if practice_question:
                return {'id': practice_question['id'], 'is_advance': False, 'type': 'practice'}
            # No practice question found - fall through to standard logic or return None
            return None

        # --- B. MODO MIXED (Controlador de Volumen) ---
        # Prioridad: Nuevas primero (hasta limit_new), luego repasos por estabilidad (hasta limit_reviews)
        if study_mode == 'MIXED':
            limit_new = st.session_state.get('limit_new', 50)
            limit_reviews = st.session_state.get('limit_reviews', 30)
            new_delivered = st.session_state.get('new_delivered', 0)
            reviews_delivered = st.session_state.get('reviews_delivered', 0)
            
            # Paso 1: Entregar nuevas primero
            if new_delivered < limit_new:
                # Buscar pregunta nueva (sin progress para este usuario)
                cursor.execute("""
                    SELECT q.id FROM questions q
                    LEFT JOIN progress p ON q.id = p.question_id AND p.username = ?
                    WHERE q.status = 'active' AND p.question_id IS NULL
                    ORDER BY RANDOM() LIMIT 1
                """, (username,))
                new_q = cursor.fetchone()
                if new_q:
                    return {'id': new_q['id'], 'is_advance': False, 'type': 'new'}
            
            # Paso 2: Entregar repasos ordenados por estabilidad (menor = más urgente)
            if reviews_delivered < limit_reviews:
                cursor.execute("""
                    SELECT q.id FROM questions q
                    JOIN progress p ON q.id = p.question_id
                    WHERE p.username = ? AND q.status = 'active' AND p.due_date <= ?
                    ORDER BY p.stability ASC
                    LIMIT 1
                """, (username, today))
                rev_q = cursor.fetchone()
                if rev_q:
                    return {'id': rev_q['id'], 'is_advance': False, 'type': 'review'}
            
            # Límites alcanzados o no hay más preguntas
            return None

        # --- C. MODO LEARN (Solo Nuevas) ---
        if study_mode == 'LEARN':
            cursor.execute("""
                SELECT q.id FROM questions q
                LEFT JOIN progress p ON q.id = p.question_id AND p.username = ?
                WHERE q.status = 'active' AND p.question_id IS NULL
                ORDER BY RANDOM() LIMIT 1
            """, (username,))
            new_q = cursor.fetchone()
            if new_q:
                return {'id': new_q['id'], 'is_advance': False, 'type': 'new'}
            return None

        # --- D. MODO REVIEW (Solo Repasos) ---
        if study_mode == 'REVIEW':
            cursor.execute("""
                SELECT q.id FROM questions q
                JOIN progress p ON q.id = p.question_id
                WHERE p.username = ? AND q.status = 'active' AND p.due_date <= ?
                ORDER BY p.stability ASC LIMIT 1
            """, (username, today))
            rev_q = cursor.fetchone()
            if rev_q:
                return {'id': rev_q['id'], 'is_advance': False, 'type': 'review'}
            return None

        # --- E. Lógica STANDARD/AUTO (Mezcla Inteligente) ---
            # Intento 1: Preguntas Vencidas (due) y Nuevas (new)
            query_priority = """
                SELECT q.id
                FROM questions q
                LEFT JOIN progress p ON q.id = p.question_id AND p.username = ?
                WHERE
                    q.status = 'active' AND (p.due_date <= ? OR p.question_id IS NULL)
                ORDER BY
                    CASE WHEN p.due_date <= ? THEN 0 ELSE 1 END, -- Vencidas (0) antes que Nuevas (1)
                    p.due_date ASC -- Las más vencidas primero
                LIMIT 1
            """
            cursor.execute(query_priority, (username, today, today))
            question = cursor.fetchone()
            if question:
                return {'id': question['id'], 'is_advance': False}

            # Intento 2: Adelantos Inteligentes (preguntas futuras)
            query_advance = """
                SELECT q.id
                FROM questions q
                JOIN progress p ON q.id = p.question_id
                WHERE
                    p.username = ? AND q.status = 'active' AND p.due_date > ?
                    AND (p.last_review IS NULL OR p.last_review != ?)
                ORDER BY p.due_date ASC -- Las que vencen más pronto primero
                LIMIT 1
            """
            cursor.execute(query_advance, (username, today, today))
            question = cursor.fetchone()
            if question:
                return {'id': question['id'], 'is_advance': True}
                
        return None
    finally:
        conn.close()
