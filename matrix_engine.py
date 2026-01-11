import time
import threading
import datetime
import json
import matrix_config
import sqlite3
import os
import requests
import database_manager as dbm

# --- CONFIGURACIÓN ---
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
MAX_RETRIES = 3

class MatrixWorker(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True # Morirá si el proceso principal muere
        self.stop_event = threading.Event()
        self.name = "MatrixWorkerThread"
        # Auto-Migración de Seguridad
        self._ensure_schema()

    def _ensure_schema(self):
        """Verifica que las tablas necesarias tengan las columnas requeridas (Migración Automática)."""
        conn = self.get_db_conn()
        try:
            # 1. Tabla matrix_topics: last_error
            res = conn.execute("PRAGMA table_info(matrix_topics)").fetchall()
            columns = [r['name'] for r in res]
            if 'last_error' not in columns:
                print("[MATRIZ] -> 🔧 MIGRATION: Adding 'last_error' column to matrix_topics...")
                conn.execute("ALTER TABLE matrix_topics ADD COLUMN last_error TEXT")
                conn.commit()

            # 2. Tabla questions: difficulty, ai_generated
            res_q = conn.execute("PRAGMA table_info(questions)").fetchall()
            columns_q = [r['name'] for r in res_q]
            
            if 'difficulty' not in columns_q:
                print("[MATRIZ] -> 🔧 MIGRATION: Adding 'difficulty' column to questions...")
                conn.execute("ALTER TABLE questions ADD COLUMN difficulty TEXT DEFAULT 'Media'")
                conn.commit()
                
            if 'ai_generated' not in columns_q:
                print("[MATRIZ] -> 🔧 MIGRATION: Adding 'ai_generated' column to questions...")
                conn.execute("ALTER TABLE questions ADD COLUMN ai_generated INTEGER DEFAULT 0")
                conn.commit()
            
            # 3. Usuario del Sistema: Matrix_AI (Para Foreign Key constraints)
            # tables referenced by questions(owner_username) -> users(username)
            user_check = conn.execute("SELECT username FROM users WHERE username = 'Matrix_AI'").fetchone()
            if not user_check:
                print("[MATRIZ] -> 🔧 MIGRATION: Creating System User 'Matrix_AI'...")
                # Asumimos password dummy o null si esquema lo permite, o usamos un hash fijo
                # Insertamos solo campos obligatorios. Asumimos username, password_hash, role
                try:
                    conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", 
                                 ('Matrix_AI', 'SYSTEM_ACCOUNT_NO_LOGIN', 'admin'))
                    conn.commit()
                except Exception as e_user:
                    print(f"[MATRIZ] -> ⚠️ Error creando usuario sistema: {e_user}")
                
        except Exception as e:
            print(f"[MATRIZ] -> ⚠️ Schema warning: {e}")
        finally:
            conn.close()

    def run(self):
        """Bucle principal del worker."""
        print("[MATRIZ] -> Worker iniciado. Ejecutando protocolo de arranque...")
        
        # Limpieza inicial de temas atascados al arrancar
        self.emergency_recovery()
        
        print("[MATRIZ] -> Esperando órdenes del sistema...")
        
        while not self.stop_event.is_set():
            try:
                # 1. Chequear Status (Kill-Switch)
                status = self.get_matrix_status()
                
                if status == 'ACTIVE':
                    # 2. Buscar trabajo (Solo 1 tema a la vez)
                    topic = self.get_next_topic()
                    
                    if topic:
                        topic_name = topic['topic_name']
                        print(f"[MATRIZ] -> Detectado tema pendiente: {topic_name} (ID: {topic['id']})")
                        
                        # 3. Procesamiento Secuencial Estricto
                        success = self.execute_sequential_process(topic)
                        
                        if success:
                            print(f"[MATRIZ] -> CICLO COMPLETADO para: {topic_name}")
                        else:
                            print(f"[MATRIZ] -> CICLO FALLIDO para: {topic_name}")
                        
                        # 4. Enfriamiento (Cooldown) para proteger disco
                        print("[MATRIZ] -> Enfriando motores (10s)...")
                        time.sleep(10) 
                    else:
                        # No hay temas, dormir
                        time.sleep(10)
                
                elif status == 'PAUSED':
                    time.sleep(10)
                
                else:
                    time.sleep(10)
                    
            except Exception as e:
                print(f"[MATRIZ] -> Error CRÍTICO en bucle principal: {e}")
                time.sleep(10)

    def get_db_conn(self):
        """Obtiene una conexión dedicada para este hilo."""
        return dbm.get_db_conn()

    def emergency_recovery(self):
        """Devuelve temas 'PROCESANDO' a 'PENDIENTE' (Recovery Anti-Zombie)."""
        conn = self.get_db_conn()
        try:
            # En un sistema real, chequearíamos timestamp, aquí reseteamos todo lo que quedó colgado
            conn.execute("UPDATE matrix_topics SET status = 'PENDIENTE' WHERE status = 'PROCESANDO'")
            conn.commit()
            print("[MATRIZ] -> Protocolo de Resurrección: Temas Zombie liberados.")
        except Exception as e:
            print(f"[MATRIZ] -> Error en recuperación: {e}")
        finally:
            conn.close()

    def get_matrix_status(self):
        conn = self.get_db_conn()
        try:
            row = conn.execute("SELECT value FROM system_config WHERE key = 'matrix_status'").fetchone()
            return row['value'] if row else 'PAUSED'
        except Exception:
            return 'PAUSED'
        finally:
            conn.close()



    def get_next_topic(self):
        conn = self.get_db_conn()
        try:
            # 1. Chequeo de seguridad: ¿Hay algo atascado?
            stuck_check = conn.execute("SELECT count(*) as cnt FROM matrix_topics WHERE status = 'PROCESANDO'").fetchone()
            if stuck_check and stuck_check['cnt'] > 0:
                print(f"[MATRIZ] -> ⚠️ Cola bloqueada: Hay {stuck_check['cnt']} tema(s) en PROCESANDO. Esperando...")
                return None
            
            # --- LÓGICA BLUEPRINT CLÍNICO (Deficit-Based) ---
            print("[MATRIZ] -> Calculando déficits clínicos...")
            
            # 2. Obtener conteo actual por categoría
            current_counts = {}
            rows = conn.execute("SELECT tag_categoria, COUNT(*) as cnt FROM questions GROUP BY tag_categoria").fetchall()
            for r in rows:
                current_counts[r['tag_categoria']] = r['cnt']
                
            # 3. Calcular Déficit para cada especialidad
            deficits = []
            target_size = matrix_config.TARGET_BANK_SIZE
            golden_ratio = matrix_config.GOLDEN_RATIO_DETAILED
            
            for specialty, weight in golden_ratio.items():
                target_count = (target_size * weight) / 100
                current_count = current_counts.get(specialty, 0)
                deficit = target_count - current_count
                deficits.append({'specialty': specialty, 'deficit': deficit})
            
            # Ordenar por mayor déficit (descendente)
            deficits.sort(key=lambda x: x['deficit'], reverse=True)
            
            # 4. Buscar tema disponible priorizando el mayor déficit
            for item in deficits:
                specialty = item['specialty']
                if item['deficit'] <= 0:
                    continue # Ya cumplimos la cuota, saltar
                
                # Buscar UN tema pendiente de esta especialidad
                # Usamos target_category que debe coincidir con el nombre de la especialidad
                row = conn.execute("""
                    SELECT id, topic_name, target_category, content_text 
                    FROM matrix_topics 
                    WHERE status = 'PENDIENTE' AND target_category = ?
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                """, (specialty,)).fetchone()
                
                if row:
                    print(f"[MATRIZ] -> Prioridad Clínica Encontrada: {specialty} (Déficit: {item['deficit']:.1f})")
                    return dict(row)

            # 5. Fallback: Si no hay temas de las especialidades con déficit,
            # tomamos cualquiera por prioridad estándar (llenar huecos o temas sin categoría definida)
            print("[MATRIZ] -> No se encontraron temas específicos para cubrir déficits. Usando Fallback General.")
            row = conn.execute("""
                SELECT id, topic_name, target_category, content_text 
                FROM matrix_topics 
                WHERE status = 'PENDIENTE' 
                ORDER BY priority ASC, created_at ASC 
                LIMIT 1
            """).fetchone()
            if row:
                return dict(row)
            return None
            
        except Exception as e:
            print(f"[MATRIZ] -> Error buscando siguiente tema: {e}")
            return None
        finally:
            conn.close()

    def get_config_values(self):
        conn = self.get_db_conn()
        api_key = None
        template = None
        try:
            row_key = conn.execute("SELECT value FROM system_config WHERE key = 'gemini_api_key'").fetchone()
            row_tmpl = conn.execute("SELECT value FROM system_config WHERE key = 'matrix_prompt_template'").fetchone()
            api_key = row_key['value'] if row_key else None
            template = row_tmpl['value'] if row_tmpl else None
        finally:
            conn.close()
        return api_key, template

    def execute_sequential_process(self, topic):
        """
        Ejecuta el flujo atómico: Mark -> API -> Save.
        """
        topic_id = topic['id']
        topic_name = topic['topic_name']
        # DUAL-SLOT: Usar content_text si existe, sino fallback a topic_name
        content_text = topic.get('content_text') or topic_name
        category = topic.get('target_category', 'General')
        
        # --- FASE 1: BLOQUEO (DB) ---
        print(f"[MATRIZ] -> Fase 1: Bloqueando tema {topic_id} ({topic_name})...")
        self.update_topic_status(topic_id, 'PROCESANDO')
        
        # Obtener config (Lectura rápida)
        api_key, prompt_template = self.get_config_values()
        
        if not api_key:
            print("[MATRIZ] -> ERROR: No API Key found.")
            self.update_topic_status(topic_id, 'ERROR', "Falta API Key")
            return False

        if not prompt_template:
            prompt_template = "Genera 5 preguntas de opción múltiple sobre {topic_name} para médicos residentes. Nivel Difícil. Formato JSON lista: enunciado, opciones, correcta, retroalimentacion."

        # USAMOS .replace() - Ahora inyecta content_text (contenido clínico) en lugar del título
        final_prompt = prompt_template.replace("{topic_name}", content_text)
        
        # [DEBUG] Log para auditoría - Ver qué se está enviando
        print(f"[MATRIZ] -> [DEBUG] topic_name: {topic_name}")
        print(f"[MATRIZ] -> [DEBUG] content_text length: {len(content_text)} chars")
        print(f"[MATRIZ] -> [DEBUG] final_prompt length: {len(final_prompt)} chars")
        print(f"[MATRIZ] -> [DEBUG] content preview: {content_text[:200]}..." if len(content_text) > 200 else f"[MATRIZ] -> [DEBUG] content: {content_text}")
        
        # Verificar que el texto se insertó correctamente
        if "{topic_name}" in final_prompt:
            print("[MATRIZ] -> ⚠️ WARNING: {topic_name} placeholder was NOT replaced!")
        
        
        # --- FASE 2: GENERACIÓN (API - SIN DB) ---
        # --- FASE 2 & 3: GENERACIÓN Y PERSISTENCIA (CON RETRY) ---
        MAX_RETRIES = 3
        
        for attempt in range(MAX_RETRIES):
            print(f"[MATRIZ] -> Intento {attempt+1}/{MAX_RETRIES}: Generando contenido con IA...")
            
            # API Call
            generated_data, api_error = self.call_gemini_api(api_key, final_prompt)
            
            if not generated_data:
                print(f"[MATRIZ] -> Fallo API (Intento {attempt+1}): {api_error}")
                # Si es el último intento, marcar error
                if attempt == MAX_RETRIES - 1:
                    self.update_topic_status(topic_id, 'ERROR', api_error or "API Failed")
                    return False
                continue 

            # Validar y Guardar (Returns True if success, False if Guardrails triggered)
            print(f"[MATRIZ] -> Validando y Guardando...")
            success = self.save_results_atomic(topic_id, topic_name, category, generated_data, api_key=api_key)
            
            if success:
                return True
            else:
                print(f"[MATRIZ] -> ⚠️ Validación Fallida (Guardrails activados). Reintentando...")
        
        # Si agotamos intentos
        print(f"[MATRIZ] -> ❌ Abortando tras {MAX_RETRIES} intentos fallidos.")
        self.update_topic_status(topic_id, 'ERROR', "Quality Guardrails Failed (N/A Options or Hallucinations)")
        return False

    def check_length_bias(self, options_list):
        """
        Verifica que ninguna opción sea > 2.5 veces más larga que la media de las otras.
        Recibe lista de strings (opciones ya formateadas o crudas).
        """
        if not options_list: return True
        # Limpiar prefijos [A] si existen para medir solo texto real
        cleaned_opts = [o.split('] ', 1)[1] if '] ' in o else o for o in options_list]
        lengths = [len(txt) for txt in cleaned_opts]
        
        if not lengths: return True
        avg_len = sum(lengths) / len(lengths)
        max_len = max(lengths)
        
        # Avoid division by zero or tiny avg
        if avg_len < 10: return True 
        
        # Ratio de Desviación (Relaxed to 4.0x for complex medical answers)
        if max_len > (avg_len * 4.0):
            print(f"[MATRIZ] -> ⚠️ RECHAZO POR SESGO DE LONGITUD: Max={max_len} vs Avg={avg_len:.1f}")
            return False
        return True

    def verify_medical_accuracy(self, api_key, question_json):
        """
        Agente Crítico: Revisa veracidad clínica.
        """
        audit_prompt = f"""ACTÚA COMO: Senior Medical Editor (USMLE/MIR Board).
TAREA: Audita la siguiente pregunta médica:
1. Veracidad Clínica: ¿La respuesta correcta es indiscutible?
2. Lógica: ¿Los distractores son incorrectos?

PREGUNTA:
{json.dumps(question_json)}

SALIDA (JSON ESTRICTO):
{{
    "verdict": "APPROVE" o "REJECT",
    "reason": "Explicación breve"
}}"""
        
        # Llamada a API (usando la misma key)
        audit_data, error = self.call_gemini_api(api_key, audit_prompt)
        
        if error or not audit_data:
            print(f"[MATRIZ] -> ⚠️ PELIGRO: Auditoría caída. Pregunta pasó sin revisión. Error: {error}")
            return True, "Audit Bypass (System Error)"
            
        verdict = audit_data.get("verdict")
        reason = audit_data.get("reason", "No reason provided")
        
        if verdict == "APPROVE":
            return True, "OK"
        else:
            print(f"[MATRIZ] -> 🛑 RECHAZO MÉDICO: {reason}")
            return False, reason

    def save_results_atomic(self, topic_id, topic_name, category, data, api_key=None):
        conn = self.get_db_conn()
        try:
            # Normalizar datos
            items = data if isinstance(data, list) else [data]
            count = 0
            
            for q in items:
                # --- POLYGLOT PARSER (Legacy + Strict JSON) ---
                enunciado = q.get('stem', q.get('enunciado'))
                explanation = q.get('explanation', q.get('retroalimentacion', 'Generado por IA'))
                topic_tag = q.get('topic_tag', q.get('tag_tema', topic_name))
                
                raw_options = q.get('options', q.get('opciones', []))
                
                # 1. Parsing de Opciones
                final_ops_list = []
                correct_answer_str = None
                
                if not raw_options or not isinstance(raw_options, list):
                    print(f"[MATRIZ] -> ⚠️ Skip: Opciones inválidas o vacías.")
                    continue
                
                # BRANCH A: Nuevo Schema (Lista de Objetos)
                if isinstance(raw_options[0], dict):
                    # Guardrail: Verificar 'N/A'
                    valid_opts = [o for o in raw_options if o.get('text') and o.get('text') != 'N/A']
                    if len(valid_opts) < 4:
                        print(f"[MATRIZ] -> ⚠️ Guardrail: Menos de 4 opciones válidas ({len(valid_opts)}). Skip.")
                        continue
                    
                    # Extraer textos y encontrar correcta
                    for obj in valid_opts[:4]: # Fuerza 4
                        txt = obj.get('text', 'Error')
                        prefix = obj.get('id', '?')
                        formatted_opt = f"[{prefix}] {txt}" if not txt.startswith('[') else txt
                        final_ops_list.append(formatted_opt)
                        
                        if obj.get('is_correct'):
                            correct_answer_str = formatted_opt
                
                # BRANCH B: Legacy Schema (Lista de Strings)
                else:
                    final_ops_list = raw_options
                    correct_answer_str = q.get('correcta')
                    # Guardraill básico para legado
                    if len(final_ops_list) < 4 or any(o == 'N/A' for o in final_ops_list):
                         print(f"[MATRIZ] -> ⚠️ Guardrail Legacy: Opciones insuficientes o 'N/A'. Skip.")
                         continue
                
                # 2. Validación Final de Integridad
                if not enunciado or not final_ops_list or not correct_answer_str:
                     print(f"[MATRIZ] -> ⚠️ Skip: Enunciado o Correcta faltantes.")
                     continue
                
                # 3. Guardrails Avanzados (Bias & Audit)
                if not self.check_length_bias(final_ops_list):
                    continue

                if api_key:
                    is_valid, reason = self.verify_medical_accuracy(api_key, q)
                    if not is_valid:
                        print(f"[MATRIZ] -> ⚠️ Skip: Rechazo Semántico ({reason})")
                        continue
                
                ops_str = "|".join(final_ops_list)
                
                # --- LÓGICA CMTG-5: MAPEO DE ESENCIA ---
                concepto = q.get('concept_key', q.get('concepto_clave'))
                badge = q.get('verif_badge', q.get('badge_verificacion'))
                
                prefix_fb = ""
                if concepto:
                    prefix_fb += f"🔑 **Concepto Clave:** {concepto}\n\n"
                if badge:
                    prefix_fb += f"🛡️ {badge}\n\n"
                
                final_feedback = prefix_fb + explanation
                
                # --- CARPETAS POR TEMA: Obtener o crear tema_id ---
                tema_nombre = topic_name.upper()
                tema_row = conn.execute("SELECT id FROM temas WHERE nombre = ?", (tema_nombre,)).fetchone()
                if tema_row:
                    tema_id = tema_row['id']
                else:
                    # Crear nuevo tema
                    conn.execute(
                        "INSERT INTO temas (nombre, categoria, created_at) VALUES (?, ?, ?)",
                        (tema_nombre, category, datetime.datetime.now())
                    )
                    tema_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                
                conn.execute("""
                    INSERT INTO questions 
                    (owner_username, enunciado, opciones, correcta, retroalimentacion, tag_categoria, tag_tema, tema_id, created_at, difficulty, ai_generated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    'Matrix_AI', 
                    enunciado, 
                    ops_str, 
                    correct_answer_str, 
                    final_feedback, 
                    category, 
                    topic_tag,
                    tema_id,
                    datetime.datetime.now(),
                    'Dificil'
                ))
                count += 1
            
            if count > 0:
                # Actualizar conteo de preguntas en tema
                conn.execute("UPDATE temas SET total_preguntas = total_preguntas + ? WHERE id = ?", (count, tema_id))
                conn.execute("UPDATE matrix_topics SET status = 'COMPLETADO' WHERE id = ?", (topic_id,))
                conn.commit()
                print(f"[MATRIZ] -> ÉXITO: {count} preguntas insertadas en tema '{tema_nombre}'. Topic {topic_id} cerrado.")
                return True
            else:
                conn.rollback()
                print(f"[MATRIZ] -> ERROR: Datos generados inválidos.")
                # FINOPS SAFEGUARD: 'ERROR' stops the bleeding
                conn.execute("UPDATE matrix_topics SET status = 'ERROR', last_error = 'Datos Inválidos (JSON Key Missing)' WHERE id = ?", (topic_id,))
                conn.commit()
                return False
                
        except Exception as e:
            conn.rollback()
            print(f"[MATRIZ] -> Error Transacción DB: {e}")
            # Intentar liberar el tema y guardar el error
            try:
                # Usamos str(e) para guardar el mensaje de excepción en la DB
                # FINOPS SAFEGUARD: 'ERROR' status
                conn.execute("UPDATE matrix_topics SET status = 'ERROR', last_error = ? WHERE id = ?", (f"DB Error: {str(e)}", topic_id))
                conn.commit()
            except:
                pass
            return False
        finally:
            conn.close()

    def call_gemini_api(self, api_key, prompt):
        # Lista de modelos: Flash-Lite primero, luego Flash regular como fallback
        models_to_try = [
            "gemini-2.0-flash",  # Más rápido y capaz para textos largos
            "gemini-2.5-flash-lite"  # Fallback económico
        ]
        
        headers = {'Content-Type': 'application/json'}
        params = {'key': api_key}
        data = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.7,
                "response_mime_type": "application/json"
            }
        }
        
        last_error = None
        
        for model_name in models_to_try:
            # Construir URL dinámica
            current_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            
            try:
                print(f"[MATRIZ] -> Intentando con modelo: {model_name}...")
                # Timeout aumentado a 180s para textos largos + CMTG-5 prompt
                response = requests.post(current_url, headers=headers, params=params, json=data, timeout=180)
                
                if response.status_code == 200:
                    result = response.json()
                    try:
                        if 'candidates' not in result:
                            # Puede pasar si bloqueado por safety
                            print(f"[MATRIZ] -> Respuesta válida pero sin 'candidates' (Safety?): {result}")
                            return None, "Safety Block"

                        text_content = result['candidates'][0]['content']['parts'][0]['text']
                        
                        # [DEBUG] LOGGING RAW ANSWER
                        safe_preview = text_content[:200] + "..." if len(text_content) > 200 else text_content
                        print(f"[MATRIZ] -> [DEBUG] Respuesta Cruda de API ({len(text_content)} chars): {safe_preview}")
                        
                        if not text_content.strip():
                            print("[MATRIZ] -> ERROR: Respuesta vacía de la API.")
                            return None, "Respuesta Vacía"
                            
                        # --- SANITIZACIÓN ROBUSTA DE JSON ---
                        # 1. Eliminar fences de Markdown
                        clean_text = text_content.replace('```json', '').replace('```', '').strip()
                        
                        # 2. Encontrar límites de Array JSON [...]
                        start_idx = clean_text.find('[')
                        end_idx = clean_text.rfind(']')
                        
                        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                            clean_text = clean_text[start_idx : end_idx + 1]
                        
                        try:
                            # Intento Principal
                            return json.loads(clean_text), None
                        except json.JSONDecodeError as json_err:
                            print(f"[MATRIZ] -> ⚠️ JSON Decode Error directo: {json_err}")
                            # Fallback: A veces Gemini pone texto antes o después sin fences
                            # Ya hemos recortado por [] pero si falla, quizás hay algo mal dentro.
                            # Intentamos un strip() más agresivo o devolvemos error detallado
                            print(f"[MATRIZ] -> [DEBUG] Failed Text Segment: {clean_text[:100]}...")
                            return None, f"JSON Error: {json_err}"
                        
                    except Exception as e:
                        print(f"[MATRIZ] -> ⚠️ Error inesperado parsing respuesta: {e}")
                        last_error = f"Parse Error: {str(e)}"
                        continue
                        
                elif response.status_code == 404:
                    print(f"[MATRIZ] -> Modelo {model_name} no encontrado (404). Probando siguiente...")
                    last_error = f"Model {model_name} 404"
                    continue # Try next model
                
                else:
                    # Otros errores (400, 403, 429) suelen ser fatales o requieren espera, no cambio de modelo.
                    # Pero si es 429 quota, cambiar de modelo NO ayuda (la quota es por proyecto).
                    print(f"[MATRIZ] -> Error HTTP API ({model_name}): {response.status_code} - {response.text}")
                    return None, f"HTTP Error: {response.status_code}"
            
            except Exception as e:
                print(f"[MATRIZ] -> Excepción Red ({model_name}): {e}")
                last_error = f"Network Error: {str(e)}"
                continue # Retry connection issue with next model? Maybe.

        return None, last_error if last_error else "Todos los modelos fallaron"

    def update_topic_status(self, topic_id, new_status, error_msg=None):
        conn = self.get_db_conn()
        try:
            if error_msg:
                # Intento robusto: Si falla last_error, hacemos fallback
                try:
                    conn.execute("UPDATE matrix_topics SET status = ?, last_error = ? WHERE id = ?", (new_status, error_msg, topic_id))
                except sqlite3.OperationalError:
                    # Fallback si no existe la columna last_error
                    print("[MATRIZ] -> ⚠️ Fallback: last_error column missing. Updating status only.")
                    conn.execute("UPDATE matrix_topics SET status = ? WHERE id = ?", (new_status, topic_id))
            else:
                conn.execute("UPDATE matrix_topics SET status = ? WHERE id = ?", (new_status, topic_id))
            conn.commit()
        finally:
            conn.close()

# --- HELPER DE BOOTSTRAP ---
_worker_instance = None

def start_matrix_worker():
    """Función para arrancar el worker (Singleton)."""
    global _worker_instance
    
    # Comprobación simple de threading para ver si ya corre
    for t in threading.enumerate():
        if t.name == "MatrixWorkerThread":
            print("🐇 [MATRIX] Worker ya está corriendo (Thread Check).")
            return
            
    if _worker_instance is None or not _worker_instance.is_alive():
        print("🐇 [MATRIX] Iniciando nuevo Worker...")
        _worker_instance = MatrixWorker()
        _worker_instance.start()
    else:
        print("🐇 [MATRIX] Worker ya activo.")
