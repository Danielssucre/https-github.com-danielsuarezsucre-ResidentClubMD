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

def get_next_question_for_user(username, practice_mode=False): # practice_mode es ahora ignorado
    """
    Obtiene la próxima pregunta para el usuario, fusionando Evaluación y Práctica en un Flujo Infinito.
    También soporta el modo de práctica por temas de la Biblioteca como una entrada prioritaria.
    
    Jerarquía del Flujo Infinito:
    1. Vencidas/Nuevas -> 2. Adelantos Futuros -> 3. Aleatorio (Respaldo).
    
    Devuelve un diccionario {'id': question_id, 'is_advance': bool} o None si no hay preguntas.
    """
    conn = dbm.get_db_conn()
    cursor = conn.cursor()
    today = datetime.date.today()

    try:
        # --- MODO PRIORITARIO: Práctica por Tema (de la Biblioteca) ---
        # Se mantiene esta funcionalidad ya que es una selección explícita del usuario
        if st.session_state.get('practice_mode'):
            practice_question = None
            
            # Caso A: Tag Único (Legacy o específico)
            if st.session_state.get('selected_tag'):
                tag = st.session_state.selected_tag
                cursor.execute(
                    "SELECT id FROM questions WHERE tag_tema = ? AND status = 'active' ORDER BY RANDOM() LIMIT 1",
                    (tag,)
                )
                practice_question = cursor.fetchone()
                
            # Caso B: Especialidad Completa (Dinámica y Escalable)
            elif st.session_state.get('practice_specialty'):
                specialty = st.session_state.practice_specialty
                # --- AUDITORIA: Filtro de exclusión de sesión ---
                answered_ids = st.session_state.get('session_answered_ids', [])
                
                # Normalización en Caliente: WHERE LOWER(tag_categoria) LIKE LOWER('Neurolog%')
                # Generamos el patrón de búsqueda (ej: "neurolog%")
                clean_spec = specialty.lower().replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u')
                # Heurística: Tomar los primeros 6 caracteres si es largo, o todo si es corto
                search_root = clean_spec[:6] if len(clean_spec) > 6 else clean_spec
                search_pattern = f"{search_root}%"
                
                params = [search_pattern]
                exclude_clause = ""
                
                if answered_ids:
                    placeholders_exclude = ','.join(['?'] * len(answered_ids))
                    exclude_clause = f"AND id NOT IN ({placeholders_exclude})"
                    params.extend(answered_ids)
                
                # Consulta simplificada y directa a la categoría (Herencia Automática)
                # FIX: Usar ORM o query segura
                query = f"SELECT id FROM questions WHERE LOWER(tag_categoria) LIKE ? {exclude_clause} AND status = 'active' ORDER BY RANDOM() LIMIT 1"
                cursor.execute(query, params)
                practice_question = cursor.fetchone()

            if practice_question:
                return {'id': practice_question['id'], 'is_advance': False}
            
            # Si estamos en modo práctica pero no hay pregunta, retornamos None
            return None

        # --- Paso A: Detectar Modo de Usuario & Intensivo ---
        try:
            # Se agrega is_intensive al SELECT
            user_row = cursor.execute("SELECT strategy_mode, is_intensive FROM users WHERE username = ?", (username,)).fetchone()
            strategy_mode = user_row['strategy_mode'] if user_row else 'STANDARD'
            # Si es None o 0 se toma como False
            is_intensive = bool(user_row['is_intensive']) if (user_row and user_row['is_intensive']) else False
        except Exception:
            strategy_mode = 'STANDARD'
            is_intensive = False

        # Lógica Maestra: Es MAFU si tiene el modo explícito O si es Intensivo
        use_mafu_logic = (strategy_mode == 'MAFU') or is_intensive

        # --- Paso B: Lógica MAFU (Si mode == 'MAFU') ---
        if use_mafu_logic:
            # B1. Deuda de Memoria (FSRS)
            # Busca en tabla progress donde due_date <= hoy. Ordena por due_date ASC.
            query_debt = """
                SELECT q.id
                FROM questions q
                JOIN progress p ON q.id = p.question_id
                WHERE p.username = ? AND q.status = 'active' AND p.due_date <= ?
                ORDER BY p.due_date ASC
                LIMIT 1
            """
            cursor.execute(query_debt, (username, today))
            question = cursor.fetchone()
            if question:
                return {'id': question['id'], 'is_advance': False}

            # B2. Avance Fractal (Material Nuevo)
            # Usa random.choices usando los pesos de GOLDEN_RATIO_DETAILED.
            topics = list(GOLDEN_RATIO_DETAILED.keys())
            weights = list(GOLDEN_RATIO_DETAILED.values())
            selected_topic = random.choices(topics, weights=weights, k=1)[0]

            query_new_base = """
                SELECT q.id 
                FROM questions q
                LEFT JOIN progress p ON q.id = p.question_id AND p.username = ?
                WHERE q.status = 'active' AND p.question_id IS NULL
            """
            
            params = [username]

            if selected_topic == 'RESTO_DEL_MUNDO':
                # Busca una pregunta activa donde el tag_tema NO ESTÉ en las claves principales
                # Excluimos usando los prefijos de las claves principales para ser robustos
                exclusion_terms = [k for k in topics if k != "RESTO_DEL_MUNDO"]
                conditions = " AND ".join([f"(tag_categoria NOT LIKE '{term}%' AND tag_tema NOT LIKE '{term}%')" for term in exclusion_terms])
                
                query_fractal = f"{query_new_base} AND {conditions} ORDER BY RANDOM() LIMIT 1"
                cursor.execute(query_fractal, params)
            else:
                # Busca una pregunta donde tag_tema o tag_categoria coincida con el tópico seleccionado
                # La lógica de split('_') se elimina ya que las claves ahora contienen espacios y acentos correctos (ej: "Cirugía General")
                query_fractal = query_new_base + " AND (tag_tema LIKE ? OR tag_categoria LIKE ?) ORDER BY RANDOM() LIMIT 1"
                term_like = f"{selected_topic}%"
                params.extend([term_like, term_like])
                cursor.execute(query_fractal, params)

            question = cursor.fetchone()
            if question:
                return {'id': question['id'], 'is_advance': False}

            # B3. Fallback: Si no hay deuda ni preguntas nuevas del tema elegido, busca CUALQUIER pregunta nueva aleatoria.
            query_fallback_new = query_new_base + " ORDER BY RANDOM() LIMIT 1"
            cursor.execute(query_fallback_new, (username,))
            question = cursor.fetchone()
            if question:
                return {'id': question['id'], 'is_advance': False}

        # --- Paso C: Lógica STANDARD (Si mode != 'MAFU') ---
        else:
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
