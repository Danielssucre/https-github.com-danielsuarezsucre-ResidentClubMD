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
        print("🐇 [MATRIX] Worker iniciado y esperando órdenes...")
        
        # Limpieza inicial de temas atascados al arrancar
        self.reset_stuck_topics()
        
        while not self.stop_event.is_set():
            try:
                # 1. Chequear Status (Kill-Switch)
                status = self.get_matrix_status()
                
                if status == 'ACTIVE':
                    # 2. Buscar trabajo
                    topic = self.get_next_topic()
                    
                    if topic:
                        print(f"🐇 [MATRIX] Procesando tema: {topic['topic_name']} (ID: {topic['id']})")
                        success = self.process_topic(topic)
                        
                        if success:
                            print(f"✅ [MATRIX] Tema completado: {topic['topic_name']}")
                        else:
                            print(f"❌ [MATRIX] Fallo en tema: {topic['topic_name']}")
                        
                        # Pausa de cortesía para no saturar DB ni API
                        time.sleep(2) 
                    else:
                        # No hay temas, dormir un poco más
                        time.sleep(10)
                
                elif status == 'PAUSED':
                    # Dormir y volver a preguntar luego
                    time.sleep(5)
                
                else:
                    # Status desconocido
                    time.sleep(5)
                    
            except Exception as e:
                print(f"⚠️ [MATRIX] Error en bucle principal: {e}")
                time.sleep(5) # Prevenir bucle de error rápido

    def get_db_conn(self):
        """Obtiene una conexión dedicada para este hilo."""
        return dbm.get_db_conn()

    def reset_stuck_topics(self):
        """Devuelve temas 'PROCESANDO' a 'PENDIENTE' (Recovery)."""
        conn = self.get_db_conn()
        try:
            conn.execute("UPDATE matrix_topics SET status = 'PENDIENTE' WHERE status = 'PROCESANDO'")
            conn.commit()
            print("🐇 [MATRIX] Recuperación: Temas atascados devueltos a la cola.")
        except Exception as e:
            print(f"⚠️ [MATRIX] Error en recuperación: {e}")
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
        except Exception:
            return None
        finally:
            conn.close()

    def get_api_key(self):
        conn = self.get_db_conn()
        try:
            row = conn.execute("SELECT value FROM system_config WHERE key = 'gemini_api_key'").fetchone()
            return row['value'] if row else None
        finally:
            conn.close()
            
    def get_prompt_template(self):
        conn = self.get_db_conn()
        try:
            row = conn.execute("SELECT value FROM system_config WHERE key = 'matrix_prompt_template'").fetchone()
            return row['value'] if row else None
        finally:
            conn.close()

    def process_topic(self, topic):
        """Orquesta la generación para un tema."""
        topic_id = topic['id']
        topic_name = topic['topic_name']
        category = topic.get('target_category', 'General')
        
        print(f"🐇 [MATRIX] Iniciando tema: {topic_name}")
        
        # 1. Marcar como PROCESANDO (Independiente para bloqueo visual)
        self.update_topic_status(topic_id, 'PROCESANDO')
        
        # 2. Obtener Credenciales y Config
        api_key = self.get_api_key()
        if not api_key:
            print("❌ [MATRIX] No API Key found.")
            # Si falta la Key, es un error de config, no tiene sentido reintentar inmediato
            self.update_topic_status(topic_id, 'ERROR', "Falta API Key")
            return False
            
        prompt_template = self.get_prompt_template()
        if not prompt_template:
            prompt_template = "Genera 1 pregunta de opción múltiple sobre {topic_name} para médicos residentes. Formato JSON: enunciado, opciones, correcta, retroalimentacion."

        # 3. Construir Prompt
        final_prompt = prompt_template.format(topic_name=topic_name)
        
        # 4. Llamar a Gemini (Sin DB Lock)
        generated_data = self.call_gemini_api(api_key, final_prompt)
        
        if not generated_data:
            # Fallo en API: Volver a PENDIENTE para reintentar luego
            print(f"⚠️ [MATRIX] Fallo API. Reencolando tema: {topic_name}")
            self.update_topic_status(topic_id, 'PENDIENTE', "Fallo API - Reintento")
            return False
            
        print(f"🐇 [MATRIX] API Respuesta recibida correctamente.")
            
        # 5. TRANSACCIÓN ATÓMICA FINAL (Guardar + Completar)
        conn = self.get_db_conn()
        try:
            # Normalizar datos
            data = generated_data if isinstance(generated_data, list) else [generated_data]
            count = 0
            
            for q in data:
                # Validar campos mínimos
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
                    'Media'
                ))
                count += 1
            
            if count > 0:
                conn.execute("UPDATE matrix_topics SET status = 'COMPLETADO' WHERE id = ?", (topic_id,))
                conn.commit() # COMMIT FINAL (TODO O NADA)
                print(f"🐇 [MATRIX] ÉXITO: Pregunta guardada e inventario actualizado ({count} preguntas).")
                return True
            else:
                conn.rollback() # No guardar nada si no hay preguntas válidas
                print(f"⚠️ [MATRIX] Datos inválidos en respuesta. Reencolando.")
                self.update_topic_status(topic_id, 'PENDIENTE', "Datos inválidos (no sc guardaron)")
                return False
                
        except Exception as e:
            conn.rollback()
            print(f"❌ [MATRIX] Error transacción final: {e}")
            # Error de base de datos podría ser temporal (Lock), reintentar
            self.update_topic_status(topic_id, 'PENDIENTE', f"DB Error: {str(e)}")
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
            response = requests.post(GEMINI_API_URL, headers=headers, params=params, json=data, timeout=30)
            if response.status_code == 200:
                result = response.json()
                try:
                    text_content = result['candidates'][0]['content']['parts'][0]['text']
                    return json.loads(text_content)
                except (KeyError, json.JSONDecodeError, IndexError) as e:
                    print(f"⚠️ [MATRIX] Error parseando respuesta JSON: {e}")
                    return None
            else:
                print(f"⚠️ [MATRIX] Error API {response.status_code}: {response.text}")
                return None
        except Exception as e:
            print(f"⚠️ [MATRIX] Excepción de red: {e}")
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
