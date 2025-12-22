import sqlite3
import google.generativeai as genai
import time
import json
import os
import random

# --- CONFIGURACIÓN DE ACCESO A LA BASE DE DATOS ---
# Esta ruta es la del disco persistente en Render.
DB_PATH_RENDER = "/opt/render/data/prisma_srs.db"
# Esta es la ruta para desarrollo local.
DB_PATH_LOCAL = "prisma_srs.db"

# Lógica de selección de BD: si estamos en Render, usamos la BD de Render.
DB_PATH = DB_PATH_RENDER if os.path.exists(DB_PATH_RENDER) else DB_PATH_LOCAL

def get_db_conn():
    """Establece una conexión con la base de datos SQLite."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Habilitar foreign keys para integridad referencial.
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

# --- FUNCIONES DE INTERACCIÓN CON GEMINI ---

def get_gemini_api_key():
    """Obtiene la clave de API de Gemini desde la base de datos."""
    print(f"📂 [DIAGNÓSTICO] Ruta absoluta de la DB: {os.path.abspath(DB_PATH)}")
    conn = None
    try:
        conn = get_db_conn()
        
        # Verificar qué hay en la tabla
        try:
            debug_cursor = conn.execute("SELECT * FROM system_config")
            rows = debug_cursor.fetchall()
            print(f"🧐 [DIAGNÓSTICO] Contenido de system_config: {[dict(row) for row in rows]}")
        except Exception as e:
            print(f"🧐 [DIAGNÓSTICO] La tabla system_config no se pudo leer: {e}")

        # 1. AUTOCURACIÓN: Crear la tabla si no existe (Igual que en app.py)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        
        # 2. Intentar leer la clave
        cursor = conn.execute("SELECT value FROM system_config WHERE key = 'gemini_api_key'")
        row = cursor.fetchone()
        
        if row:
            return row['value']
        else:
            return None # Retorna None en lugar de fallar
            
    except Exception as e:
        print(f"⚠️ Error de conexión DB: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_matrix_prompt_template():
    """
    Obtiene el prompt maestro desde la BD.
    """
    conn = get_db_conn()
    try:
        prompt_row = conn.execute("SELECT value FROM system_config WHERE key = 'matrix_prompt_template'").fetchone()
        
        # Placeholder simple por si la BD falla, así evitamos errores de sintaxis
        default_prompt = "Actúa como experto en {topic_name}. Genera 5 preguntas en JSON sobre {topic_name}."
        
        return prompt_row['value'] if prompt_row and prompt_row['value'] else default_prompt
    finally:
        if conn:
            conn.close()

def parse_and_validate_question(json_text, target_category):
    """
    Intenta parsear el JSON de la pregunta, validarlo y añadir la categoría.
    Devuelve un diccionario validado o None si falla.
    """
    try:
        # Intenta cargar el texto JSON en un diccionario Python.
        data = json.loads(json_text)
        
        # --- Validación de Campos Esenciales ---
        required_keys = ["enunciado", "opciones", "correcta", "retroalimentacion", "tag_tema"]
        for key in required_keys:
            if key not in data or not data[key]:
                print(f"❌ Error de Validación: Falta la clave '{key}' o está vacía en la pregunta generada.")
                return None
        
        # Validación específica para 'opciones'
        if not isinstance(data['opciones'], list) or len(data['opciones']) != 4:
            print("❌ Error de Validación: 'opciones' debe ser una lista de 4 strings.")
            return None
        if not all(isinstance(op, str) and op for op in data['opciones']):
            print("❌ Error de Validación: Todas las opciones deben ser strings no vacíos.")
            return None
            
        # Validación de la respuesta correcta
        if data['correcta'] not in data['opciones']:
            print("❌ Error de Validación: La respuesta 'correcta' no coincide con ninguna de las opciones.")
            return None

        # --- Asignación y Formateo ---
        # Asigna la categoría objetivo proporcionada.
        data['tag_categoria'] = target_category
        # Une las opciones en un string separado por '|' para el almacenamiento en BD.
        data['opciones_str'] = "|".join(data['opciones'])
        
        return data
        
    except json.JSONDecodeError:
        print(f"❌ Error Crítico: No se pudo decodificar el JSON. Respuesta del modelo:\n{json_text}")
        return None
    except Exception as e:
        print(f"❌ Error inesperado durante el parseo y validación: {e}")
        return None

def store_questions_in_db(questions_to_add):
    """
    Almacena una lista de preguntas validadas en la base de datos.
    Se asegura de que todas las preguntas se inserten en una única transacción.
    """
    if not questions_to_add:
        return 0

    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        # El usuario 'admin' es el propietario de las preguntas generadas por IA. 
        admin_user = "admin" 
        
        # Inicia una transacción. Si algo falla, se revierte todo.
        cursor.execute("BEGIN TRANSACTION")
        
        for q in questions_to_add:
            cursor.execute("""
                INSERT INTO questions (owner_username, enunciado, opciones, correcta, retroalimentacion, tag_categoria, tag_tema)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                admin_user,
                q['enunciado'],
                q['opciones_str'],
                q['correcta'],
                q['retroalimentacion'],
                q['tag_categoria'],
                q['tag_tema']
            ))
        
        # Si todo fue bien, confirma los cambios.
        conn.commit()
        return len(questions_to_add)
        
    except sqlite3.Error as e:
        print(f"❌ Error de Base de Datos: Falló la inserción de preguntas. Haciendo rollback... Error: {e}")
        # En caso de error, revierte todos los cambios de la transacción.
        conn.rollback()
        return 0
    finally:
        conn.close()

# --- LÓGICA PRINCIPAL DEL PROCESADOR DE LA MATRIZ ---

def process_matrix_queue():
    """
    Función principal que se ejecuta en bucle. Busca el siguiente tema en la cola,
    genera preguntas con Gemini y las guarda en la base de datos.
    """
    # --- Control de Estado ---
    conn_status = get_db_conn()
    matrix_target_id = None
    try:
        status_row = conn_status.execute("SELECT value FROM system_config WHERE key = 'matrix_status'").fetchone()
        matrix_status = status_row['value'] if status_row else 'PAUSED'
        
        # Si es una orden específica, leemos el ID objetivo
        if matrix_status == 'ONCE_SPECIFIC':
            target_row = conn_status.execute("SELECT value FROM system_config WHERE key = 'matrix_target_id'").fetchone()
            matrix_target_id = target_row['value'] if target_row else None
    except Exception:
        matrix_status = 'PAUSED'
    finally:
        conn_status.close()

    if matrix_status == 'PAUSED':
        print("💤 Esperando órdenes...")
        time.sleep(5)
        return

    print(f"🧬 Iniciando ciclo del procesador de La Matriz... (Modo: {matrix_status})")
    
    # 1. Obtener la clave de API y configurar el modelo.
    api_key = get_gemini_api_key()
    if not api_key:
        print("⏳ [ESPERANDO] No hay API Key. Ve al panel de Admin y guárdala...")
        time.sleep(5)
        return
        
    try:
        genai.configure(api_key=api_key)
        # Configuración del modelo y de seguridad.
        generation_config = {"temperature": 0.7, "top_p": 1, "top_k": 1, "max_output_tokens": 8192}
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]
        # Actualizado a Gemini 2.5 Flash (Estable dic-2025)
        model = genai.GenerativeModel('gemini-2.5-flash')
    except Exception as e:
        print(f"❌ Error configurando el modelo de Gemini: {e}")
        return

    # 2. Conectar a la BD y buscar un tema pendiente.
    conn = get_db_conn()
    try:
        # --- PHOENIX LOOP: Resurrección de Temas ---
        if matrix_status == 'RUNNING':
            pending_count = conn.execute("SELECT COUNT(*) FROM matrix_topics WHERE status = 'PENDIENTE'").fetchone()[0]
            if pending_count == 0:
                print("🔄 Ronda finalizada. Reactivando todos los temas en COOLDOWN (y COMPLETADO)...")
                conn.execute("UPDATE matrix_topics SET status = 'PENDIENTE' WHERE status = 'COOLDOWN' OR status = 'COMPLETED'")
                conn.commit()

        topic_row = None
        
        # CASO 2: Status == 'ONCE_SPECIFIC' (Tu orden manual)
        if matrix_status == 'ONCE_SPECIFIC':
            if matrix_target_id:
                print(f"🎯 Buscando objetivo específico ID: {matrix_target_id}")
                topic_row = conn.execute("SELECT id, topic_name, target_category FROM matrix_topics WHERE id = ?", (matrix_target_id,)).fetchone()
            
            if not topic_row:
                print(f"⚠️ Orden manual fallida: No se encontró el tema ID {matrix_target_id} o no es válido.")
                conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
                conn.commit()
                return
        else:
            # CASO 3: Status == 'RUNNING' (Modo Industrial / DNA Helix)
            # La consulta busca un tema que esté 'PENDIENTE', ordenado por prioridad y luego aleatorio (DNA).
            topic_row = conn.execute("""
                SELECT id, topic_name, target_category FROM matrix_topics
                WHERE status = 'PENDIENTE'
                ORDER BY priority ASC, RANDOM()
                LIMIT 1
            """).fetchone()

        if not topic_row:
            print("✅ No hay temas pendientes en la cola. Ciclo finalizado.")
            if matrix_status in ['ONCE', 'ONCE_SPECIFIC']:
                conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
                conn.commit()
            return

        topic_id = topic_row['id']
        topic_name = topic_row['topic_name']
        target_category = topic_row['target_category']

        # Fallback por si la categoría es nula en la BD
        if not target_category:
            print(f"⚠️ Alerta: El tema '{topic_name}' (ID: {topic_id}) no tiene categoría de destino. Se omitirá.")
            conn.execute("UPDATE matrix_topics SET status = 'ERROR' WHERE id = ?", (topic_id,))
            conn.commit()
            return
        
        print(f"⏳ Procesando tema '{topic_name}' (ID: {topic_id}) para la categoría '{target_category}'...")
        # Marca el tema como 'PROCESANDO' para evitar que otro worker lo tome.
        conn.execute("UPDATE matrix_topics SET status = 'PROCESANDO' WHERE id = ?", (topic_id,))
        conn.commit()

        # 3. Construir el prompt para la IA.
        # Obtiene la plantilla de prompt desde la base de datos.
        prompt_template = get_matrix_prompt_template()
        # Inyecta el nombre del tema y la categoría en la plantilla.
        prompt = prompt_template.replace("{topic_name}", topic_name).replace("{target_category}", target_category)
        print("📡 Usando Prompt dinámico desde la base de datos...")
        
        questions_to_add = []
        
        # Bucle de Reintento (Retry Loop)
        for attempt in range(3):
            try:
                # 4. Llamar a la API de Gemini.
                response = model.generate_content(prompt)
                
                # 5. Procesar la respuesta.
                if not response.parts:
                    raise ValueError(f"La respuesta del modelo para '{topic_name}' está vacía.")
                    
                # Extrae el contenido de texto de la respuesta.
                generated_text = response.text
                
                # Limpia el texto para asegurar que sea un JSON válido (a veces el modelo añade ```json ... ```)
                if generated_text.strip().startswith("```json"):
                    clean_json_text = generated_text.strip()[7:-4].strip()
                else:
                    clean_json_text = generated_text.strip()
                    
                # 6. Parsear y validar las preguntas.
                generated_questions = json.loads(clean_json_text)
                
                if not isinstance(generated_questions, list):
                    raise ValueError("El JSON generado no es una lista.")

                temp_questions = []
                for q_data in generated_questions:
                    validated_q = parse_and_validate_question(json.dumps(q_data), target_category)
                    if validated_q:
                        temp_questions.append(validated_q)
                
                if not temp_questions:
                    raise ValueError("No se pudieron validar preguntas del JSON generado.")

                questions_to_add = temp_questions
                break # Éxito, salir del bucle

            except (json.JSONDecodeError, ValueError, Exception) as e:
                print(f"⚠️ Intento {attempt+1}/3 fallido: {e}. Reintentando...")
                if attempt < 2:
                    time.sleep(2)
                else:
                    print(f"❌ Fallaron los 3 intentos para '{topic_name}'.")
                    conn.execute("UPDATE matrix_topics SET status = 'ERROR' WHERE id = ?", (topic_id,))
                    conn.commit()
                    return

        # 7. Almacenar las preguntas y actualizar el estado del tema.
        if questions_to_add:
            num_added = store_questions_in_db(questions_to_add)
            print(f"✅ Se almacenaron {num_added} preguntas nuevas para el tema '{topic_name}'.")
            if num_added > 0:
                if matrix_status == 'RUNNING':
                    conn.execute("UPDATE matrix_topics SET status = 'COOLDOWN' WHERE id = ?", (topic_id,))
                else:
                    conn.execute("UPDATE matrix_topics SET status = 'COMPLETED' WHERE id = ?", (topic_id,))
                
                # Lógica 'ONCE' y 'ONCE_SPECIFIC'
                if matrix_status in ['ONCE', 'ONCE_SPECIFIC']:
                    conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
                    print("✅ Orden manual completada. Sistema Pausado.")
            else:
                # Si hubo un error en la transacción, se marca como error.
                conn.execute("UPDATE matrix_topics SET status = 'ERROR' WHERE id = ?", (topic_id,))
                if matrix_status == 'ONCE_SPECIFIC':
                    conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
                    conn.commit()
        else:
            print(f"⚠️ No se generaron preguntas válidas para '{topic_name}'. Marcando como error.")
            conn.execute("UPDATE matrix_topics SET status = 'ERROR' WHERE id = ?", (topic_id,))
            if matrix_status == 'ONCE_SPECIFIC':
                conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
            
        conn.commit()

    except Exception as e:
        print(f"❌ Ocurrió un error inesperado en el ciclo principal: {e}")
        # Si estamos procesando un tema, lo marcamos como error para que no bloquee la cola.
        if 'topic_id' in locals() and topic_id:
            conn.execute("UPDATE matrix_topics SET status = 'ERROR' WHERE id = ?", (topic_id,))
            conn.commit()
    finally:
        conn.close()

# --- BUCLE DE EJECUCIÓN CONTINUA ---
if __name__ == "__main__":
    print("Iniciando el servicio de La Matriz en modo autónomo.")
    while True:
        process_matrix_queue()
        # Espera aleatoria para no sobrecargar la API y parecer más 'humano'.
        sleep_time = random.uniform(10, 25)
        print(f"--- Ciclo completo. Durmiendo por {sleep_time:.1f} segundos... ---")
        time.sleep(sleep_time)
