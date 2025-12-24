import time
import threading
import datetime
import json
import sqlite3
import os
import requests
import database_manager as dbm

# --- CONFIGURACIÓN ---
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
MAX_RETRIES = 3

class MatrixWorker(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True # Morirá si el proceso principal muere
        self.stop_event = threading.Event()
        self.name = "MatrixWorkerThread"

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
            # Si hay algún tema en 'PROCESANDO', NO tocar nada más hasta que se resuelva.
            stuck_check = conn.execute("SELECT count(*) as cnt FROM matrix_topics WHERE status = 'PROCESANDO'").fetchone()
            if stuck_check and stuck_check['cnt'] > 0:
                print(f"[MATRIZ] -> ⚠️ Cola bloqueada: Hay {stuck_check['cnt']} tema(s) en PROCESANDO. Esperando...")
                return None

            # Prioridad 1 (Crítica) primero, luego por fecha
            row = conn.execute("""
                SELECT id, topic_name, target_category 
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
        category = topic.get('target_category', 'General')
        
        # --- FASE 1: BLOQUEO (DB) ---
        print(f"[MATRIZ] -> Fase 1: Bloqueando tema {topic_id}...")
        self.update_topic_status(topic_id, 'PROCESANDO')
        
        # Obtener config (Lectura rápida)
        api_key, prompt_template = self.get_config_values()
        
        if not api_key:
            print("[MATRIZ] -> ERROR: No API Key found.")
            self.update_topic_status(topic_id, 'ERROR', "Falta API Key")
            return False

        if not prompt_template:
            prompt_template = "Genera 5 preguntas de opción múltiple sobre {topic_name} para médicos residentes. Nivel Difícil. Formato JSON lista: enunciado, opciones, correcta, retroalimentacion."

        # Modificación para forzar 5 preguntas si el template no lo especifica
        # O confiar en el template del usuario. Asumimos el template del usuario o el default.
        # USAMOS .replace() en lugar de .format() para evitar KeyError si el prompt tiene JSON brackets {}
        final_prompt = prompt_template.replace("{topic_name}", topic_name)
        
        # --- FASE 2: GENERACIÓN (API - SIN DB) ---
        print(f"[MATRIZ] -> Fase 2: Generando contenido con IA...")
        generated_data = self.call_gemini_api(api_key, final_prompt)
        
        if not generated_data:
            print(f"[MATRIZ] -> Fallo API. Liberando tema...")
            self.update_topic_status(topic_id, 'PENDIENTE', "Fallo API")
            return False
            
        # --- FASE 3: PERSISTENCIA (DB - ATÓMICA) ---
        print(f"[MATRIZ] -> Fase 3: Escribiendo en disco rígido...")
        return self.save_results_atomic(topic_id, topic_name, category, generated_data)

    def save_results_atomic(self, topic_id, topic_name, category, data):
        conn = self.get_db_conn()
        try:
            # Normalizar datos
            items = data if isinstance(data, list) else [data]
            count = 0
            
            for q in items:
                # Validar campos
                if not all(k in q for k in ('enunciado', 'opciones', 'correcta')):
                    continue
                
                ops = q['opciones']
                ops_str = "|".join(ops) if isinstance(ops, list) else str(ops)
                
                conn.execute("""
                    INSERT INTO questions 
                    (owner_username, enunciado, opciones, correcta, retroalimentacion, tag_categoria, tag_tema, created_at, difficulty, ai_generated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    'Matrix_AI', 
                    q['enunciado'], 
                    ops_str, 
                    q['correcta'], 
                    q.get('retroalimentacion', 'Generado por IA'), 
                    category, 
                    topic_name, 
                    datetime.datetime.now(),
                    'Dificil'
                ))
                count += 1
            
            if count > 0:
                conn.execute("UPDATE matrix_topics SET status = 'COMPLETADO' WHERE id = ?", (topic_id,))
                conn.commit()
                print(f"[MATRIZ] -> ÉXITO: {count} preguntas insertadas. Tema {topic_id} cerrado.")
                return True
            else:
                conn.rollback()
                print(f"[MATRIZ] -> ERROR: Datos generados inválidos.")
                conn.execute("UPDATE matrix_topics SET status = 'PENDIENTE', last_error = 'Datos Inválidos' WHERE id = ?", (topic_id,))
                conn.commit()
                return False
                
        except Exception as e:
            conn.rollback()
            print(f"[MATRIZ] -> Error Transacción DB: {e}")
            # Intentar liberar el tema
            try:
                conn.execute("UPDATE matrix_topics SET status = 'PENDIENTE' WHERE id = ?", (topic_id,))
                conn.commit()
            except:
                pass
            return False
        finally:
            conn.close()

    def call_gemini_api(self, api_key, prompt):
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
        
        try:
            print("[MATRIZ] -> Enviando request a Gemini API...")
            response = requests.post(GEMINI_API_URL, headers=headers, params=params, json=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                try:
                    text_content = result['candidates'][0]['content']['parts'][0]['text']
                    
                    # [DEBUG] LOGGING RAW ANSWER
                    safe_preview = text_content[:200] + "..." if len(text_content) > 200 else text_content
                    print(f"[MATRIZ] -> [DEBUG] Respuesta Cruda de API ({len(text_content)} chars): {safe_preview}")
                    
                    if not text_content.strip():
                        print("[MATRIZ] -> ERROR: Respuesta vacía de la API.")
                        return None
                        
                    return json.loads(text_content)
                except Exception as e:
                    print(f"[MATRIZ] -> Error JSON Parse: {e}. Contenido: {text_content[:100]}...")
                    return None
            else:
                print(f"[MATRIZ] -> Error HTTP API: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            print(f"[MATRIZ] -> Excepción Red: {e}")
            return None

    def update_topic_status(self, topic_id, new_status, error_msg=None):
        conn = self.get_db_conn()
        try:
            if error_msg:
                conn.execute("UPDATE matrix_topics SET status = ?, last_error = ? WHERE id = ?", (new_status, error_msg, topic_id))
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
