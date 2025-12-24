import streamlit as st
import sqlite3
import pandas as pd
import datetime
import os
import time
import json
import io
import altair as alt
import random
import math
import plotly.express as px
from passlib.context import CryptContext  # Para hashing de contraseñas
import numpy as np
import shutil
import re
import traceback

# --- MÓDULOS DEPLOY V1 ---
import database_manager as dbm
import auth_handler as auth
import ui_components as ui
from fsrs_engine import FSRS_v5_Engine, get_next_question_for_user
import matrix_engine as matrix # Nuevo módulo Worker
from matrix_config import GOLDEN_RATIO_DETAILED # Configuración de pesos clínicos

def clean_ai_prefixes(text):
    """
    Elimina prefijos como 'A.', 'A)', '[A]', '(A)' al inicio del texto
    para evitar doble etiquetado.
    """
    # Regex: Busca A-D o a-d seguido de punto, paréntesis o corchetes al inicio
    return re.sub(r'^\[?[A-Da-d]\]?[\.\)\s]+', '', text).strip()

# --- CONFIGURACIÓN DE PÁGINA ---
st.set_page_config(
    page_title="ResidentClubMD",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- INICIALIZACIÓN DE SESIÓN (MODO SEGURO) ---
auth.init_session()

# Contexto para hashear contraseñas
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# --- CONTROL DE CACHÉ DE SESIÓN (FIX RETENCIÓN) ---
if 'cache_cleared_session' not in st.session_state:
    st.cache_data.clear()
    st.session_state.cache_cleared_session = True

# --- CONEXIÓN BASE DE DATOS (DELEGADA) ---
# Usamos el manager para garantizar WAL y Rutas correctas en Render
def get_db_conn():
    return dbm.get_db_conn()

# --- MATRIX WORKER BOOTSTRAP (SINGLETON) ---
@st.cache_resource
def ensure_matrix_schema():
    conn = get_db_conn()
    try:
        # Check if 'last_error' column exists
        res = conn.execute("PRAGMA table_info(matrix_topics)").fetchall()
        columns = [r['name'] for r in res]
        if 'last_error' not in columns:
            print("🔧 [MIGRATION] Adding 'last_error' column to matrix_topics...")
            conn.execute("ALTER TABLE matrix_topics ADD COLUMN last_error TEXT")
            conn.commit()
    except Exception as e:
        print(f"⚠️ Schema warning: {e}")
    finally:
        conn.close()

def bootstrap_matrix():
    """Arranque seguro del hilo de La Matriz."""
    ensure_matrix_schema()
    matrix.start_matrix_worker()

# Lanzar inmediatamente al cargar el script (st.cache_resource evita duplicados)
bootstrap_matrix()

# ==========================================
# ==========================================

# --- FUNCIONES BACKEND: MODO INVITADO (FREE TIER) ---

def get_user_ip():
    """
    Obtiene la IP del usuario usando el handler robusto (Render compatible).
    """
    if 'user_ip_stable' not in st.session_state:
        st.session_state.user_ip_stable = auth.get_remote_ip()
    return st.session_state.user_ip_stable

def check_guest_access():
    """
    Consulta la BD para validar acceso gratuito.
    Retorna: (puede_jugar: bool, mensaje: str, requiere_encuesta: bool)
    """
    ip = get_user_ip()
    today = datetime.date.today()
    conn = get_db_conn()
    
    try:
        # 1. Verificar si existe perfil (Encuesta realizada)
        profile = conn.execute("SELECT * FROM guest_profiles WHERE ip_address = ?", (ip,)).fetchone()
        if not profile:
            return False, "Encuesta requerida", True
            
        # 2. Verificar límites diarios
        limit_row = conn.execute(
            "SELECT questions_used FROM daily_limits WHERE ip_address = ? AND usage_date = ?", 
            (ip, today)
        ).fetchone()
        
        used = limit_row['questions_used'] if limit_row else 0
        
        if used >= 5:
            return False, "🔒 Límite diario alcanzado (5/5). Inicia sesión para continuar.", False
            
        return True, f"✅ Modo Invitado: {5 - used} preguntas restantes hoy.", False
    except Exception as e:
        print(f"Error check_guest_access: {e}")
        return False, "Error de sistema", False
    finally:
        conn.close()

def register_guest_survey(admitted, attempts):
    """Guarda los datos de la encuesta inicial del invitado."""
    ip = get_user_ip()
    conn = get_db_conn()
    try:
        # admitted maps to is_resident (1/0)
        is_res = 1 if admitted else 0
        conn.execute(
            "INSERT OR REPLACE INTO guest_profiles (ip_address, is_resident, attempts_count) VALUES (?, ?, ?)",
            (ip, is_res, attempts)
        )
        conn.commit()
    finally:
        conn.close()

def increment_guest_usage():
    """Suma +1 al contador de uso diario para la IP actual."""
    ip = get_user_ip()
    today = datetime.date.today()
    conn = get_db_conn()
    try:
        conn.execute("""
            INSERT INTO daily_limits (ip_address, usage_date, questions_used)
            VALUES (?, ?, 1)
            ON CONFLICT(ip_address, usage_date) DO UPDATE SET
            questions_used = questions_used + 1
        """, (ip, today))
        conn.commit()
    finally:
        conn.close()

def log_mining_data(question_id, is_correct):
    """Registra datos de rendimiento para minería de datos (Fase 3)."""
    ip = get_user_ip()
    conn = get_db_conn()
    try:
        # Obtenemos si ya ha pasado el examen desde la encuesta guardada
        profile = conn.execute("SELECT is_resident FROM guest_profiles WHERE ip_address = ?", (ip,)).fetchone()
        has_passed = profile['is_resident'] if profile else 0
        
        conn.execute("""
            INSERT INTO performance_mining (ip_address, question_id, is_correct, has_passed_exam)
            VALUES (?, ?, ?, ?)
        """, (ip, str(question_id), is_correct, has_passed))
        conn.commit()
    finally:
        conn.close()

def get_ghost_profile():
    # Devuelve el diccionario del Usuario Fantasma (Referencia) o None.
    conn = get_db_conn()
    # Buscamos al usuario marcado como modelo (1)
    try:
        row = conn.execute("SELECT * FROM users WHERE is_reference_model = 1 LIMIT 1").fetchone()
        if row:
            # Convertimos el objeto sqlite3.Row a un diccionario normal
            return dict(row)
    except Exception as e:
        print(f"Error buscando fantasma: {e}")
    return None

def init_mafu_curriculum(conn):
    """
    Inyecta el Cronograma Maestro de 260 temas con Alta Precisión.
    Se ejecuta en setup_database para garantizar alineación total.
    """
    cursor = conn.cursor()
    
    # Definición de la tabla de temas si no existe
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY,
            especialidad TEXT NOT NULL,
            subespecialidad TEXT NOT NULL,
            nombre_tema TEXT NOT NULL
        )
    """)
    
    # Verificar si ya existen datos para no ralentizar el inicio innecesariamente
    # (Aunque para 'Alta Precisión' forzaremos la actualización si el conteo difiere)
    count = cursor.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    
    # Lista Maestra de Temas (MAFU High Precision - Versión Definitiva)
    new_topics_data = [
        ('Cardiología', '1. Falla cardíaca crónica: introducción y recomendaciones'),
        ('Cardiología', '2. Falla cardíaca crónica: abordaje, escenarios y manejo'),
        ('Cardiología', '3. Falla cardíaca crónica: puntos de controversia y manejo médico de la angina de pecho'),
        ('Cardiología', '4. Falla cardíaca crónica: definición, epidemiología y etiología'),
        ('Cardiología', '5. Falla cardíaca crónica: signos, síntomas y ayudas diagnósticas'),
        ('Cardiología', '6. Falla cardíaca crónica: clasificación, tratamiento y dosis de medicamentos'),
        ('Cardiología', '7. Falla cardíaca crónica: dispositivos'),
        ('Cardiología', '8. Falla cardíaca aguda: definición, epidemiología y fisiopatología'),
        ('Cardiología', '9. Falla cardíaca aguda: etiología, diagnóstico y tratamiento'),
        ('Cardiología', '10. Falla cardíaca aguda: uso de diuréticos y otros escenarios clínicos'),
        ('Cardiología', '11. Bases del electrocardiograma'),
        ('Cardiología', '12. Anatomía en el electrocardiograma'),
        ('Cardiología', '13. Interpretación básica: frecuencia cardíaca, ritmos, eje, segmentos e intervalos'),
        ('Cardiología', '14. Dilatación de ventrículos'),
        ('Cardiología', '15. Dilatación de aurículas'),
        ('Cardiología', '16. Bloqueos de rama'),
        ('Cardiología', '17. Bloqueos fasciculares'),
        ('Cardiología', '18. Bloqueos trifasciculares'),
        ('Cardiología', '19. Síndrome coronario'),
        ('Cardiología', '20. Anatomía coronaria'),
        ('Cardiología', '21. Elevación del segmento del intervalo ST'),
        ('Cardiología', '22. Patrones de alto riesgo'),
        ('Cardiología', '23. Taquiarritmias de complejos estrechos'),
        ('Cardiología', '24. Taquicardias atriales'),
        ('Cardiología', '25. Fibrilación auricular'),
        ('Cardiología', '26. Flutter auricular'),
        ('Cardiología', '27. Reentradas y vías accesorias'),
        ('Cardiología', '28. Extrasístoles ventriculares'),
        ('Cardiología', '29. Taquicardias ventriculares'),
        ('Cardiología', '30. Bradiarritmias'),
        ('Cardiología', '31. Disfunción del nodo sinusal'),
        ('Cardiología', '32. Bloqueo auriculoventricular de primer grado'),
        ('Cardiología', '33. Bloqueos auriculoventriculares de segundo grado'),
        ('Cardiología', '34. Bloqueo auriculoventricular de tercer grado'),
        ('Cardiología', '35. Síndrome de Brugada'),
        ('Cardiología', '36. Tromboembolismo pulmonar agudo'),
        ('Cardiología', '37. Pericarditis'),
        ('Cardiología', '38. Síndrome de Wolff-Parkinson-White'),
        ('Cardiología', '39. Displasia arritmogénica del ventrículo derecho'),
        ('Cardiología', '40. Miocardiopatía hipertrófica del ventrículo izquierdo'),
        ('Cardiología', '41. Manejo de marcapasos'),
        ('Cardiología', '42. Guía de riesgo cardiovascular'),
        ('Cardiología', '43. Hipertensión arterial'),
        ('Cardiología', '44. Dislipidemia'),
        ('Cardiología', '45. Síncope: diagnóstico y tratamiento'),
        ('Cardiología', '46. Fibrilación atrial: definición, fisiopatología y manifestaciones clínicas'),
        ('Cardiología', '47. Fibrilación atrial: examen físico, diagnóstico y manejo íntegro'),
        ('Cardiología', '48. Fibrilación atrial: tratamiento y conclusiones'),
        ('Infectología', '49. Coinfección por VIH y Tuberculosis: ¿Cuál es la terapia antirretroviral ideal?'),
        ('Infectología', '50. Coinfección por VIH y Virus de la Hepatitis B'),
        ('Infectología', '51. Tuberculosis'),
        ('Infectología', '52. Endocarditis infecciosa'),
        ('Infectología', '53. Neumonía adquirida en la comunidad: generalidades, clínica, diagnóstico y tratamiento'),
        ('Infectología', '54. Malaria'),
        ('Infectología', '55. Dengue, chikungunya y zika'),
        ('Infectología', '56. Fiebre de origen desconocido: definición, fisiopatología y enfoque'),
        ('Infectología', '57. Enfermedad diarreica aguda'),
        ('Infectología', '58. Vacunación en el adulto: esquemas y casos especiales'),
        ('Endocrinología', '59. Diabetes Mellitus: Generalidades'),
        ('Endocrinología', '60. Hipotiroidismo'),
        ('Endocrinología', '61. Diabetes Mellitus: definición, epidemiología y ayudas diagnósticas'),
        ('Endocrinología', '62. Diabetes Mellitus: tratamiento y seguimiento'),
        ('Endocrinología', '63. Nódulo tiroideo: clínica, ecografía y sistema Bethesda'),
        ('Endocrinología', '64. Tirotoxicosis: manifestaciones, diagnóstico y tratamiento'),
        ('Endocrinología', '65. Osteoporosis: fisiopatología, diagnóstico y tratamiento'),
        ('Hematología', '66. Anemias: generalidades y anemia de células falciformes'),
        ('Hematología', '67. Neoplasia hematológica: enfoque del paciente'),
        ('Hematología', '68. INR supraterapéutico y sangrado por warfarina'),
        ('Hematología', '69. Escenarios en antiagregación plaquetaria'),
        ('Nefrología', '70. Enfermedad renal crónica: definición, fisiopatología y manejo'),
        ('Nefrología', '71. Enfermedad glomerular'),
        ('Nefrología', '72. Enfermedad renal aguda: abordaje en sepsis y rabdomiólisis'),
        ('Nefrología', '73. Glomerulonefritis: presentación clínica'),
        ('Nefrología', '74. Nefropatía diabética'),
        ('Nefrología', '75. Infección del tracto urinario: diagnóstico y tratamiento'),
        ('Nefrología', '76. Síndrome cardiorrenal'),
        ('Nefrología', '77. Trastornos hidroelectrolíticos: Sodio, Potasio y Calcio'),
        ('Gastroenterología', '78. Interpretación de pruebas hepáticas'),
        ('Gastroenterología', '79. Hígado graso: valoración inicial y tratamiento'),
        ('Gastroenterología', '80. Cirrosis hepática: complicaciones generales'),
        ('Gastroenterología', '81. Ictericia: enfoque clínico y paraclínicos'),
        ('Gastroenterología', '82. Enfermedad de reflujo gastroesofágico'),
        ('Neumología', '83. Derrame pleural: fisiopatología, diagnóstico y algoritmos'),
        ('Neumología', '84. EPOC ambulatorio: clasificación GOLD y tratamiento'),
        ('Neumología', '85. EPOC exacerbado: indicaciones de hospitalización y soporte'),
        ('Neumología', '86. Tromboembolismo pulmonar: escala de Wells y abordaje'),
        ('Neumología', '87. Nódulo pulmonar'),
        ('Neumología', '88. Enfermedad pulmonar difusa: enfoque diagnóstico'),
        ('Reumatología', '89. Interpretación de pruebas en reumatología'),
        ('Reumatología', '90. Vasculitis: enfoque del paciente con vasculitis sistémicas'),
        ('Reumatología', '91. Lupus eritematoso sistémico'),
        ('Reumatología', '92. Artritis reumatoide'),
        ('Reumatología', '93. Gota'),
        ('Reumatología', '94. Miopatía: enfoque y diagnóstico'),
        ('Reumatología', '95. Espóndilodiscitis'),
        ('Vascular', '96. Trombosis venosa profunda'),
        ('Vascular', '97. Insuficiencia venosa crónica'),
        ('Vascular', '98. Enfermedad arterial periférica'),
        ('Cirugía General', '99. Politrauma: enfoque inicial ABCDE'),
        ('Cirugía General', '100. Trauma de cuello: zonas anatómicas y manejo'),
        ('Cirugía General', '101. Trauma de tórax: neumotórax'),
        ('Cirugía General', '102. Trauma de tórax: hemotórax y taponamiento cardíaco'),
        ('Cirugía General', '103. Trauma de tórax: contusión pulmonar y grandes vasos'),
        ('Cirugía General', '104. Herida precordial: enfoque y seguimiento'),
        ('Cirugía General', '105. Trauma toracoabdominal'),
        ('Cirugía General', '106. Trauma de abdomen: abordaje'),
        ('Cirugía General', '107. Trauma vascular en extremidades'),
        ('Cirugía General', '108. Aneurisma de aorta abdominal'),
        ('Cirugía General', '109. Hemorragia de vías digestivas altas'),
        ('Cirugía General', '110. Hemorragia de vías digestivas inferiores'),
        ('Cirugía General', '111. Isquemia intestinal'),
        ('Cirugía General', '112. Urgencias quirúrgicas abdominales del recién nacido'),
        ('Cirugía General', '113. Infecciones necrotizantes de piel y tejidos blandos'),
        ('Cirugía General', '114. Urgencias anorrectales'),
        ('Cirugía General', '115. Enfermedad diverticular y diverticulitis'),
        ('Cirugía General', '116. Apendicitis aguda: escalas e imágenes'),
        ('Cirugía General', '117. Colecistitis, coledocolitiasis y colangitis'),
        ('Cirugía General', '118. Pancreatitis aguda: etiología y manejo'),
        ('Cirugía General', '119. Obstrucción del intestino delgado'),
        ('Cirugía General', '120. Absceso hepático'),
        ('Cirugía General', '121. Enfermedad inflamatoria intestinal'),
        ('Cirugía General', '122. Tamizaje de cáncer colorrectal'),
        ('Cirugía General', '123. Cáncer gástrico'),
        ('Cirugía General', '124. Nódulo tiroideo quirúrgico: Bethesda'),
        ('Cirugía General', '125. Enfoque de masas en cuello en el adulto'),
        ('Cirugía General', '126. Enfermedad ácida péptica y Helicobacter pylori'),
        ('Cirugía General', '127. ERGE: tratamiento farmacológico y quirúrgico'),
        ('Cirugía General', '128. Hernia diafragmática'),
        ('Cirugía General', '129. Hernia de la pared abdominal'),
        ('Cirugía General', '130. Infección de sitio operatorio'),
        ('Cirugía General', '131. Disfagia aguda y no aguda'),
        ('Ortopedia', '132. Radiología básica en ortopedia'),
        ('Ortopedia', '133. Fracturas de clavícula y húmero proximal'),
        ('Ortopedia', '134. Fracturas diafisiarias y húmero distal'),
        ('Ortopedia', '135. Fracturas de radio y ulna'),
        ('Ortopedia', '136. Fracturas de pelvis y fémur'),
        ('Ortopedia', '137. Fracturas de tibia, peroné y pie'),
        ('Ortopedia', '138. Fracturas abiertas: tratamiento'),
        ('Ortopedia', '139. Esguinces y luxaciones'),
        ('Ortopedia', '140. Trauma en mano'),
        ('Ortopedia', '141. Cojera en niños: abordaje clínico'),
        ('Ortopedia', '142. Fracturas en pediatría: fisis y crecimiento'),
        ('Ortopedia', '143. Trastornos rotacionales en niños'),
        ('Ortopedia', '144. Pie diabético: clasificación y tratamiento'),
        ('Ortopedia', '145. Síndrome compartimental'),
        ('Ortopedia', '146. Infecciones osteoarticulares pediátricas'),
        ('Ortopedia', '147. Infecciones osteoarticulares en adultos'),
        ('Ortopedia', '148. Tumores óseos: enfoque general'),
        ('Ortopedia', '149. Dermatomas y miotomas'),
        ('Ortopedia', '150. Dolor cervicobraquial y lumbar agudo'),
        ('Ortopedia', '151. Neuropatías por atrapamiento'),
        ('Urología', '152. Cáncer de próstata: detección y tratamiento'),
        ('Urología', '153. Cáncer de vejiga'),
        ('Urología', '154. Tumores renales y testiculares'),
        ('Urología', '155. Urgencias urológicas no traumáticas: torsión y priapismo'),
        ('Urología', '156. Urgencias urológicas traumáticas'),
        ('Urología', '157. Hiperplasia prostática benigna'),
        ('Otorrinolaringología', '158. Urgencias nasales, otológicas y faríngeas'),
        ('Otorrinolaringología', '159. Otitis media aguda y con efusión'),
        ('Otorrinolaringología', '160. Hipoacusia: estudios y tamizaje neonatal'),
        ('Otorrinolaringología', '161. Vértigo: VPPB y síndrome vestibular agudo'),
        ('Urgencias', '162. RCP: compresiones, ritmos y cuidados post-paro'),
        ('Urgencias', '163. Arritmias cardíacas en urgencias'),
        ('Urgencias', '164. SCA: ECG de alto riesgo e infarto ST/No ST'),
        ('Urgencias', '165. Crisis hiperglucémicas: abordaje y tratamiento'),
        ('Urgencias', '166. Coma mixedematoso'),
        ('Urgencias', '167. Crisis tiroideas'),
        ('Urgencias', '168. Insuficiencia adrenal'),
        ('Urgencias', '169. Urgencias en cirrosis hepática: várices, encefalopatía y peritonitis'),
        ('Urgencias', '170. Urgencias en falla hepática aguda'),
        ('Urgencias', '171. Urgencias dialíticas'),
        ('Urgencias', '172. Urgencias oncológicas: vena cava y lisis tumoral'),
        ('Urgencias', '173. Compresión medular'),
        ('Urgencias', '174. Sepsis: protocolos Surviving Sepsis'),
        ('Urgencias', '175. Tromboembolismo pulmonar en urgencias'),
        ('Psiquiatría', '176. Psicosis y esquizofrenia'),
        ('Psiquiatría', '177. Trastorno por déficit de atención e hiperactividad'),
        ('Psiquiatría', '178. Trastornos de la conducta alimentaria'),
        ('Psiquiatría', '179. Trastornos neurocognoscitivos mayores'),
        ('Psiquiatría', '180. Trastorno obsesivo compulsivo'),
        ('Psiquiatría', '181. Trastorno de estrés postraumático'),
        ('Psiquiatría', '182. Trastorno afectivo bipolar'),
        ('Psiquiatría', '183. Trastornos de ansiedad'),
        ('Psiquiatría', '184. Trastornos de la personalidad: Grupos A, B y C'),
        ('Psiquiatría', '185. Conducta suicida y paciente agitado'),
        ('Psiquiatría', '186. Psicofarmacología: antidepresivos y antipsicóticos'),
        ('Psiquiatría', '187. Trastorno de insomnio'),
        ('Psiquiatría', '188. Psiquiatría infantil'),
        ('Neurología', '189. Debilidad aguda no traumática'),
        ('Neurología', '190. Cefaleas'),
        ('Neurología', '191. Parkinson y trastornos del movimiento'),
        ('Neurología', '192. Neuroinfección'),
        ('Neurología', '193. Epilepsia y estado epiléptico'),
        ('Neurología', '194. Neuralgia del trigémino'),
        ('Neurología', '195. Parálisis facial'),
        ('Neurología', '196. Síndromes neurológicos: motor, cerebeloso y meníngeo'),
        ('Neurología', '197. Vértigo neurológico'),
        ('Neurología', '198. Deterioro cognitivo'),
        ('Neurología', '199. Trastornos del sueño'),
        ('Neurología', '200. Síndrome medular no traumático'),
        ('Neurología', '201. Neuritis óptica'),
        ('Neurología', '202. ACV Isquémico y ataque isquémico transitorio'),
        ('Neurocirugía', '203. Trauma toracolumbar'),
        ('Neurocirugía', '204. Trauma raquimedular'),
        ('Neurocirugía', '205. Trauma craneoencefálico'),
        ('Neurocirugía', '206. Hemorragia intracerebral espontánea'),
        ('Neurocirugía', '207. Hemorragia subaracnoidea espontánea'),
        ('Neurocirugía', '208. Tumores del SNC: gliomas y meningiomas'),
        ('Neurocirugía', '209. Muerte encefálica'),
        ('Epidemiología', '210. Conceptos básicos y tipos de estudios'),
        ('Epidemiología', '211. Ensayos clínicos y metaanálisis'),
        ('Epidemiología', '212. Estudios descriptivos'),
        ('Epidemiología', '213. Estudios analíticos: casos, controles y cohortes'),
        ('Epidemiología', '214. Rendimiento diagnóstico: Sensibilidad y Especificidad'),
        ('Epidemiología', '215. Medicina basada en evidencia'),
        ('Pediatría', '216. Reanimación neonatal: algoritmos'),
        ('Pediatría', '217. Reanimación neonatal: intubación y medicamentos'),
        ('Pediatría', '218. Dificultad respiratoria neonatal'),
        ('Pediatría', '219. Neumonía congénita'),
        ('Pediatría', '220. Ictericia neonatal'),
        ('Pediatría', '221. Sepsis neonatal'),
        ('Pediatría', '222. Cardiopatías congénitas'),
        ('Pediatría', '223. Toxoplasmosis congénita'),
        ('Pediatría', '224. Citomegalovirus congénito'),
        ('Pediatría', '225. Sífilis congénita'),
        ('Pediatría', '226. Herpes simple neonatal'),
        ('Pediatría', '227. Vacunación pediátrica'),
        ('Pediatría', '228. Lactancia materna'),
        ('Pediatría', '229. Desnutrición infantil'),
        ('Pediatría', '230. Parasitosis intestinal'),
        ('Pediatría', '231. EDA pediátrica e hidratación'),
        ('Pediatría', '232. Anemia en pediatría'),
        ('Pediatría', '233. Tosferina y bronquiolitis'),
        ('Pediatría', '234. Asma pediátrica'),
        ('Pediatría', '235. Tuberculosis pediátrica'),
        ('Pediatría', '236. Fibrosis quística'),
        ('Pediatría', '237. ITU pediátrica'),
        ('Pediatría', '238. Síndrome nefrítico y nefrótico'),
        ('Pediatría', '239. Meningitis pediátrica'),
        ('Pediatría', '240. Crisis febriles'),
        ('Pediatría', '241. Epilepsia pediátrica'),
        ('Pediatría', '242. TEC pediátrico'),
        ('Pediatría', '243. Patología quirúrgica pediátrica'),
        ('Pediatría', '244. Fiebre sin foco'),
        ('Pediatría', '245. Sepsis pediátrica'),
        ('Pediatría', '246. Enfermedades exantemáticas'),
        ('Pediatría', '247. Enfermedad de Kawasaki'),
        ('Pediatría', '248. Hipertensión en niños'),
        ('Ginecología y Obstetricia', '249. Control prenatal'),
        ('Ginecología y Obstetricia', '250. Toxoplasmosis gestacional'),
        ('Ginecología y Obstetricia', '251. Sífilis gestacional'),
        ('Ginecología y Obstetricia', '252. Diabetes gestacional'),
        ('Ginecología y Obstetricia', '253. VIH en el embarazo'),
        ('Ginecología y Obstetricia', '254. Hemorragias del embarazo y código rojo'),
        ('Ginecología y Obstetricia', '255. Preeclampsia y eclampsia'),
        ('Ginecología y Obstetricia', '256. Amenaza de parto pretérmino'),
        ('Ginecología y Obstetricia', '257. Embarazo ectópico'),
        ('Ginecología y Obstetricia', '258. Hemorragia uterina anormal'),
        ('Ginecología y Obstetricia', '259. Tamización de cáncer de mama'),
        ('Ginecología y Obstetricia', '260. Tamización de cáncer de cérvix'),
        ('Ginecología y Obstetricia', '261. Endometriosis'),
        ('Ginecología y Obstetricia', '262. Menopausia y reemplazo hormonal'),
        ('Oftalmología', '263. Anatomía ocular'),
        ('Oftalmología', '264. Leucocoria'),
        ('Oftalmología', '265. Ojo rojo'),
        ('Oftalmología', '266. Neuropatías ópticas'),
        ('Oftalmología', '267. Enfermedad ocular tiroidea'),
        ('Oftalmología', '268. Glaucoma')
    ]
    
    # Convertir al formato de la tabla: (id, especialidad, subespecialidad, nombre_tema)
    temas_exactos = []
    for i, (cat, tema) in enumerate(new_topics_data, 1):
        temas_exactos.append((i, cat, 'General', tema))

    # Verificar si necesitamos actualizar (por conteo o por corrección de categorías)
    # Para la versión definitiva, forzamos la actualización si el conteo no coincide
    needs_update = (count != len(temas_exactos))

    if needs_update:
        print(f"⚙️ MAFU: Actualizando cronograma maestro (Versión Definitiva)...")
        # Limpieza total para evitar duplicados o IDs viejos
        cursor.execute("DELETE FROM topics")
        cursor.executemany("INSERT INTO topics (id, especialidad, subespecialidad, nombre_tema) VALUES (?, ?, ?, ?)", temas_exactos)
        conn.commit()
        print(f"🚀 ¡Cronograma Refactorizado inyectado: {len(temas_exactos)} temas!")

def setup_database():
    """
    Crea y migra la base de datos de forma segura. Verifica la existencia de todas
    las tablas y columnas necesarias y las añade si no existen.
    """
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # --- Creación de Tablas (si no existen) ---
    cursor.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user');")
    cursor.execute("CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_username TEXT NOT NULL REFERENCES users(username), enunciado TEXT NOT NULL, opciones TEXT NOT NULL, correcta TEXT NOT NULL, retroalimentacion TEXT NOT NULL, tag_categoria TEXT, tag_tema TEXT);")
    cursor.execute("CREATE TABLE IF NOT EXISTS progress (username TEXT NOT NULL REFERENCES users(username), question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE, due_date DATE NOT NULL, interval INTEGER NOT NULL DEFAULT 1, aciertos INTEGER NOT NULL DEFAULT 0, fallos INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (username, question_id));")
    cursor.execute("CREATE TABLE IF NOT EXISTS duels (id INTEGER PRIMARY KEY AUTOINCREMENT, challenger_username TEXT NOT NULL REFERENCES users(username), opponent_username TEXT NOT NULL REFERENCES users(username), question_ids TEXT NOT NULL, challenger_score INTEGER, opponent_score INTEGER, status TEXT NOT NULL, winner TEXT, created_at DATETIME NOT NULL);")
    cursor.execute("CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, action_type TEXT NOT NULL, timestamp DATETIME NOT NULL);")
    cursor.execute("CREATE TABLE IF NOT EXISTS deleted_users_log (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, deletion_date DATETIME NOT NULL, reason TEXT);")
    cursor.execute("CREATE TABLE IF NOT EXISTS question_votes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_username TEXT NOT NULL REFERENCES users(username), question_id INTEGER NOT NULL REFERENCES questions(id), vote_type INTEGER NOT NULL, timestamp DATETIME NOT NULL);")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_question_vote ON question_votes (user_username, question_id);")

    # --- INICIO: Tablas para Modo Invitado (Free Tier) ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guest_profiles (
            ip_address TEXT PRIMARY KEY,
            is_resident INTEGER DEFAULT 0,
            attempts_count INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_limits (
            ip_address TEXT,
            usage_date DATE,
            questions_used INTEGER DEFAULT 0,
            PRIMARY KEY (ip_address, usage_date)
        )
    """)
    
    # Tabla para la Bitácora de Minería (Fase 3)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS performance_mining (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            question_id TEXT,
            is_correct BOOLEAN,
            has_passed_exam BOOLEAN,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # --- FIN: Tablas para Modo Invitado ---

    # --- INICIO: Tablas para 'La Matriz' ---
    # Tabla para la cola de generación de temas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matrix_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_name TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDIENTE',
            priority INTEGER NOT NULL DEFAULT 3,
            created_at DATETIME NOT NULL
        )
    """)
    
    # Tabla para el control de la cuota de API
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_quota (
            date TEXT PRIMARY KEY,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ACTIVE'
        )
    """)

    # Tabla para sugerencias de temas (Pre-producción)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suggested_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_topic TEXT NOT NULL,
            suggester_name TEXT DEFAULT 'Anonimo',
            status TEXT DEFAULT 'PENDING', -- PENDING, APPROVED, REJECTED
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Tabla para configuraciones generales del sistema (Key-Value)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # --- Inserción de configuración por defecto ---
    cursor.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('matrix_mode', 'ASC')")
    cursor.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('last_processed_id', '0')")
    
    cursor.execute("""
        INSERT OR IGNORE INTO system_config (key, value)
        VALUES (
            'matrix_auditor_prompt',
            'Actúa como un Auditor Médico Senior.
            Tu trabajo es filtrar preguntas generadas por IA.
            
            Analiza este JSON: {question_json}
            
            REGLAS DE APROBACIÓN:
            1. Debe ser un CASO CLÍNICO (paciente, síntomas), no una pregunta directa.
            2. La respuesta debe ser 100% correcta y actualizada.
            3. No debe ser obvia ni fácil de adivinar.
            
            Responde SOLO JSON: {"verdict": "APPROVED" | "REJECTED", "reason": "...", "quality_score": 1-10}'
        )
    """)
    
    # --- INICIO: Creación y Migración de Categorías Médicas ---
    cursor.execute("CREATE TABLE IF NOT EXISTS medical_categories (name TEXT PRIMARY KEY)")

    # --- Migraciones Seguras de Columnas ---
    
    def add_column_if_not_exists(table, column_name, column_def):
        """Función auxiliar para añadir una columna de forma idempotente."""
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = [col[1] for col in cursor.fetchall()]
        if column_name not in existing_columns:
            st.warning(f"Migrando BD: Añadiendo columna '{column_name}' a tabla '{table}'...")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}")

    # Migraciones para la tabla 'users'
    add_column_if_not_exists('users', 'is_approved', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_not_exists('users', 'is_intensive', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_not_exists('users', 'max_inactivity_days', 'INTEGER NOT NULL DEFAULT 3')
    add_column_if_not_exists('users', 'status', "TEXT NOT NULL DEFAULT 'active'")
    add_column_if_not_exists('users', 'is_resident', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_not_exists('users', 'intensive_start_date', 'DATE')
    add_column_if_not_exists('users', 'total_active_days', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_not_exists('users', 'current_streak', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_not_exists('users', 'last_active_date', 'DATE')
    add_column_if_not_exists('users', 'last_streak_date', 'DATE')
    add_column_if_not_exists('users', 'is_reference_model', 'INTEGER DEFAULT 0')
    add_column_if_not_exists('users', 'final_exam_score', 'INTEGER DEFAULT NULL')
    add_column_if_not_exists('users', 'cohort_year', 'TEXT DEFAULT NULL')
    add_column_if_not_exists('users', 'target_exam_date', 'DATE DEFAULT NULL')
    add_column_if_not_exists('users', 'admitted_status', "TEXT DEFAULT 'Pending'")
    add_column_if_not_exists('users', 'admitted_specialty', 'TEXT DEFAULT NULL')
    add_column_if_not_exists('users', 'final_accuracy_snapshot', 'REAL DEFAULT 0.0')
    add_column_if_not_exists('users', 'avg_daily_questions', 'REAL DEFAULT 0.0')
    add_column_if_not_exists('users', 'avg_seconds_per_question', 'REAL DEFAULT 0.0')
    add_column_if_not_exists('users', 'total_questions_snapshot', 'INTEGER DEFAULT 0')
    add_column_if_not_exists('users', 'strategy_mode', "TEXT NOT NULL DEFAULT 'STANDARD'")
    add_column_if_not_exists('users', 'access_expiration', 'DATE DEFAULT NULL')

    # --- INICIO: Migraciones de Seguridad (Anti-Fuerza Bruta) ---
    add_column_if_not_exists('users', 'failed_attempts', 'INTEGER NOT NULL DEFAULT 0')
    add_column_if_not_exists('users', 'lockout_until', 'DATETIME DEFAULT NULL')
    # --- FIN: Migraciones de Seguridad ---

    # Migraciones para la tabla 'questions'
    add_column_if_not_exists('questions', 'status', "TEXT NOT NULL DEFAULT 'active'")
    add_column_if_not_exists('questions', 'karma', 'INTEGER NOT NULL DEFAULT 0') # Columna para Karma/Votos
    add_column_if_not_exists('questions', 'created_at', "DATETIME")
    # Backfill para asegurar que no haya fechas nulas en preguntas antiguas
    cursor.execute("UPDATE questions SET created_at = DATETIME('now') WHERE created_at IS NULL")

    # Migraciones para la tabla 'progress' (FSRS)
    add_column_if_not_exists('progress', 'stability', 'REAL NOT NULL DEFAULT 0.0')
    add_column_if_not_exists('progress', 'difficulty', 'REAL NOT NULL DEFAULT 0.0')
    add_column_if_not_exists('progress', 'retrievability', 'REAL NOT NULL DEFAULT 0.0')
    add_column_if_not_exists('progress', 'last_review', 'DATE')

    # Migracion para la tabla 'activity_log'
    add_column_if_not_exists('activity_log', 'metadata', 'TEXT')

    # Migracion para la tabla 'matrix_topics'
    add_column_if_not_exists('matrix_topics', 'target_category', 'TEXT')
    
    # --- MIGRACIÓN: Limpieza Masiva y Estandarización (Solicitada) ---
    cursor.execute("SELECT value FROM system_config WHERE key = 'migration_cleanup_v1'")
    if not cursor.fetchone():
        st.warning("⚙️ Mantenimiento: Ejecutando limpieza y estandarización de categorías...")
        
        # 1. Limpieza de Preguntas (Normalización)
        updates = [
            ('Cardiologia', ['cardiologia', 'cardiología', 'cardio']),
            ('Ginecologia', ['ginecologia', 'ginecología', 'gineco']),
            ('Pediatria', ['pediatria', 'pediatría', 'pedia']),
            ('Neumologia', ['neumologia', 'neumología', 'neumo']),
            ('Gastroenterologia', ['gastroenterologia', 'gastroenterología', 'gastro']),
            ('Neurologia', ['neurologia', 'neurología', 'neuro']),
            ('Cirugia', ['cirugia', 'cirugía', 'cirugia general']),
            ('Psiquiatria', ['psiquiatria', 'psiquiatría']),
            ('Dermatologia', ['dermatologia', 'dermatología']),
            ('Hematologia', ['hematologia', 'hematología']),
            ('Infectologia', ['infectologia', 'infectología']),
            ('Reumatologia', ['reumatologia', 'reumatología']),
            ('Ortopedia', ['traumatologia', 'traumatología', 'ortopedia']),
            ('Oftalmologia', ['oftalmologia', 'oftalmología']),
            ('Endocrinologia', ['endocrinologia', 'endocrinología']),
            ('Nefrologia', ['nefrologia', 'nefrología']),
            ('Otorrinolaringología', ['otorrino', 'otorrinolaringologia', 'orl'])
        ]
        
        for official, variants in updates:
            placeholders = ','.join(['?'] * len(variants))
            sql = f"UPDATE questions SET tag_categoria = ? WHERE tag_categoria COLLATE NOCASE IN ({placeholders})"
            cursor.execute(sql, [official] + variants)
            
        # 2. Reinicio de Tabla de Categorías (Lista Blanca)
        cursor.execute("DELETE FROM medical_categories")
        CATEGORIAS_OFICIALES = [
            'Pediatria', 'Ginecologia', 'Cardiologia', 'Neumologia', 
            'Gastroenterologia', 'Neurologia', 'Nefrologia', 'Endocrinologia', 
            'Hematologia', 'Infectologia', 'Reumatologia', 'Dermatologia', 
            'Psiquiatria', 'Ortopedia', 'Cirugia', 'Otorrinolaringología', 'Oftalmologia'
        ]
        for cat in CATEGORIAS_OFICIALES:
            cursor.execute("INSERT INTO medical_categories (name) VALUES (?)", (cat,))
            
        # Marcar migración como completada
        cursor.execute("INSERT INTO system_config (key, value) VALUES ('migration_cleanup_v1', 'done')")
        conn.commit()
        st.success("✅ Mantenimiento completado: Base de datos normalizada.")

    # --- MIGRACIÓN MAFU 2.0: Cirugía de Etiquetas y Estandarización ---
    cursor.execute("SELECT value FROM system_config WHERE key = 'migration_mafu_v2'")
    if not cursor.fetchone():
        st.warning("⚙️ MAFU 2.0: Ejecutando cirugía de etiquetas (Ortopedia, Otorrinolaringología, etc.)...")
        
        # 1. Mapa de Corrección: Viejo -> Nuevo (MAFU 2.0)
        corrections = {
            'Traumatologia': 'Ortopedia', 'Traumatología': 'Ortopedia', 'Ortopedia': 'Ortopedia',
            'Otorrino': 'Otorrinolaringología', 'Otorrinolaringologia': 'Otorrinolaringología', 'ORL': 'Otorrinolaringología',
            'Cirugia': 'Cirugía General', 'Cirugía': 'Cirugía General', 'Cirugia General': 'Cirugía General',
            'Ginecologia': 'Ginecología y Obstetricia', 'Ginecología': 'Ginecología y Obstetricia', 'Gineco': 'Ginecología y Obstetricia',
            'Pediatria': 'Pediatría',
            'Cardiologia': 'Cardiología',
            'Neumologia': 'Neumología',
            'Gastroenterologia': 'Gastroenterología',
            'Neurologia': 'Neurología',
            'Nefrologia': 'Nefrología',
            'Endocrinologia': 'Endocrinología',
            'Hematologia': 'Hematología',
            'Infectologia': 'Infectología',
            'Reumatologia': 'Reumatología',
            'Oftalmologia': 'Oftalmología',
            'Psiquiatria': 'Psiquiatría',
            'Epidemiologia': 'Epidemiología'
        }

        for old, new in corrections.items():
            # Actualizar preguntas y objetivos de la matriz
            cursor.execute("UPDATE questions SET tag_categoria = ? WHERE tag_categoria = ?", (new, old))
            cursor.execute("UPDATE matrix_topics SET target_category = ? WHERE target_category = ?", (new, old))

        # 2. Reinicio de Tabla de Categorías (Lista Oficial MAFU 2.0)
        # Esto asegura que el filtro de la biblioteca coincida exactamente con el algoritmo
        cursor.execute("DELETE FROM medical_categories")
        # La repoblación ocurrirá en el bloque siguiente (Script de migración por defecto)
        
        cursor.execute("INSERT INTO system_config (key, value) VALUES ('migration_mafu_v2', 'done')")
        conn.commit()
        st.success("✅ MAFU 2.0: Cirugía de datos completada. Etiquetas alineadas.")

    # --- MIGRACIÓN: Huérfanas de Medicina Interna ---
    cursor.execute("SELECT value FROM system_config WHERE key = 'migration_med_interna_orphans'")
    if not cursor.fetchone():
        st.warning("⚙️ Mantenimiento: Reasignando huérfanas de Medicina Interna...")
        
        mappings = {
            'Endocrinología': ['Diabetes', 'Tiroides', 'Osteoporosis'],
            'Infectología': ['VIH', 'TB', 'Sepsis', 'Dengue', 'Malaria'],
            'Nefrología': ['Falla Renal', 'Glomerulopatías', 'Electrolitos'],
            'Neumología': ['EPOC', 'Neumonía', 'Derrame Pleural'],
            'Gastroenterología': ['Cirrosis', 'Hígado', 'ERGE', 'Ictericia'],
            'Reumatología': ['Lupus', 'Artritis', 'Vasculitis'],
            'Hematología': ['Anemias', 'Coagulación'],
            'Vascular': ['Trombosis', 'Enfermedad Arterial']
        }
        
        for new_cat, keywords in mappings.items():
            for kw in keywords:
                cursor.execute(f"UPDATE questions SET tag_categoria = ? WHERE tag_categoria = 'Medicina Interna' AND tag_tema LIKE ?", (new_cat, f"%{kw}%"))
        
        cursor.execute("INSERT INTO system_config (key, value) VALUES ('migration_med_interna_orphans', 'done')")
        conn.commit()
        st.success("✅ Huérfanas de Medicina Interna reasignadas.")

    # Script de migración: poblar la tabla si está vacía
    cursor.execute("SELECT COUNT(*) FROM medical_categories")
    if cursor.fetchone()[0] == 0:
        st.warning("Migrando BD: Poblando 'medical_categories' desde la lista estática...")
        # --- FIX: Lista por defecto para inicialización ---
        # Lista alineada con la versión definitiva del temario
        CATEGORIAS_MEDICAS = [
            'Cardiología', 'Infectología', 'Endocrinología', 'Hematología', 'Nefrología', 
            'Gastroenterología', 'Neumología', 'Reumatología', 'Vascular', 'Cirugía General', 
            'Ortopedia', 'Urología', 'Otorrinolaringología', 'Urgencias', 'Psiquiatría', 
            'Neurología', 'Neurocirugía', 'Epidemiología', 'Pediatría', 
            'Ginecología y Obstetricia', 'Oftalmología'
        ]
        # ------------------------------------------------
        for category in CATEGORIAS_MEDICAS:
            cursor.execute("INSERT OR IGNORE INTO medical_categories (name) VALUES (?)", (category,))
    # --- FIN: Creación y Migración de Categorías Médicas ---
    
    # --- FIN: Tablas para 'La Matriz' ---

    # (Moves to top)

    # --- Configuración del Admin por Defecto ---
    try:
        ADMIN_USER_DEFAULT = st.secrets["ADMIN_USER"]
        ADMIN_PASS_DEFAULT = st.secrets["ADMIN_PASS"]
    except (KeyError, FileNotFoundError):
        st.error("Error crítico: Faltan ADMIN_USER o ADMIN_PASS en los secretos de Streamlit (secrets.toml).")
        st.stop()

    cursor.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER_DEFAULT,))
    admin = cursor.fetchone()
    
    if not admin:
        admin_pass_bytes = ADMIN_PASS_DEFAULT.encode('utf-8')[:72]
        admin_pass_hash = pwd_context.hash(admin_pass_bytes)
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, is_approved) VALUES (?, ?, 'admin', 1)",
            (ADMIN_USER_DEFAULT, admin_pass_hash)
        )
    else:
        # Auto-fix: Actualizar hash del admin al arrancar para evitar UnknownHashError si cambió el esquema
        admin_pass_bytes = ADMIN_PASS_DEFAULT.encode('utf-8')[:72]
        admin_pass_hash = pwd_context.hash(admin_pass_bytes)
        cursor.execute("UPDATE users SET is_approved = 1, role = 'admin', password_hash = ? WHERE username = ?", (admin_pass_hash, ADMIN_USER_DEFAULT,))

    # --- Configuración del Usuario Invitado ---
    guest_pass_hash = pwd_context.hash("nopassword")
    cursor.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, is_approved) VALUES (?, ?, 'guest', 1)",
        ('guest_mode', guest_pass_hash)
    )

    # Promover al administrador al modo estrategia MAFU
    cursor.execute("UPDATE users SET strategy_mode = 'MAFU' WHERE username = ?", (ADMIN_USER_DEFAULT,))

    # --- INYECCIÓN DE CRONOGRAMA MAESTRO (MAFU) ---
    init_mafu_curriculum(conn)

    conn.commit()
    conn.close()

# --- FUNCIONES DE AUTENTICACIÓN Y HASHING ---

def verify_password(plain_password, hashed_password):
    """Verifica la contraseña plana contra el hash."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        print(f"⚠️ Error verificando hash (posiblemente corrupto o esquema desconocido): {e}")
        return False

def get_user_role(username):
    """Obtiene el rol (admin/user) de un usuario."""
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result['role'] if result else None

def get_all_categories():
    """Obtiene una lista de todas las categorías médicas desde la base de datos."""
    conn = get_db_conn()
    try:
        rows = conn.execute("SELECT name FROM medical_categories ORDER BY name ASC").fetchall()
        # Convierte la lista de objetos Row a una lista de strings
        return [row['name'] for row in rows]
    except Exception as e:
        print(f"Error al obtener categorías: {e}")
        return [] # Devuelve una lista vacía en caso de error
    finally:
        if conn:
            conn.close()

def delete_user_from_db(username):
    """
    Elimina un usuario, transfiere sus preguntas al admin y limpia datos asociados.
    Sigue una lógica de expropiación para no perder contenido comunitario valioso.
    """
    try:
        # 1. Identificar al Admin
        admin_user = st.secrets["ADMIN_USER"]
    except KeyError:
        # Fallback para entorno local donde los secrets no están definidos
        admin_user = "admin"

    # 2. Validación: No eliminar al admin
    if username == admin_user:
        st.error(f"No se puede eliminar al usuario administrador principal ('{admin_user}').")
        return

    conn = None
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        # Iniciar transacción
        cursor.execute("BEGIN TRANSACTION")

        # 3. Limpieza de Datos Personales (Borrar)
        # Eliminar participaciones en duelos
        cursor.execute("DELETE FROM duels WHERE challenger_username = ? OR opponent_username = ?", (username, username))
        # Eliminar todo el progreso de estudio
        cursor.execute("DELETE FROM progress WHERE username = ?", (username,))
        # Eliminar todos los votos emitidos por el usuario
        cursor.execute("DELETE FROM question_votes WHERE user_username = ?", (username,))
        # Eliminar el historial de actividad
        cursor.execute("DELETE FROM activity_log WHERE username = ?", (username,))

        # 4. Preservación de Contenido (Transferir)
        # Actualizar el propietario de las preguntas para que pertenezcan al admin
        cursor.execute("UPDATE questions SET owner_username = ? WHERE owner_username = ?", (admin_user, username))

        # 5. Eliminación de Cuenta
        # Finalmente, eliminar el registro del usuario
        cursor.execute("DELETE FROM users WHERE username = ?", (username,))

        # Confirmar la transacción si todo fue exitoso
        conn.commit()

        st.success(f"Usuario '{username}' eliminado. Sus preguntas han sido transferidas al admin '{admin_user}'.")

    except sqlite3.Error as e:
        if conn:
            conn.rollback()  # Revertir cambios en caso de error de base de datos
        st.error(f"Error de base de datos al eliminar usuario: {e}")
    except Exception as e:
        if conn:
            conn.rollback()  # Revertir también en caso de otro tipo de error
        st.error(f"Ocurrió un error inesperado durante la eliminación: {e}")
    finally:
        if conn:
            conn.close()


def log_event(user_id, event_type, metadata_dict=None):
    """
    Registra un evento genérico en el activity_log con metadatos JSON.
    """
    conn = None
    try:
        # Asegura que los metadatos sean un diccionario antes de procesar
        if metadata_dict is None:
            metadata_dict = {}
        
        # Convierte el diccionario a un string JSON.
        meta_json = json.dumps(metadata_dict)
        
        conn = get_db_conn()
        cursor = conn.cursor()
        
        # Inserta el nuevo evento incluyendo los metadatos.
        cursor.execute(
            "INSERT INTO activity_log (username, action_type, timestamp, metadata) VALUES (?, ?, ?, ?)",
            (user_id, event_type, datetime.datetime.now(), meta_json)
        )
        conn.commit()
    
    except sqlite3.Error as e:
        # Error específico de la base de datos
        print(f"Error de base de datos al registrar evento: {e}")
    except TypeError as e:
        # Error durante la serialización a JSON (ej. un objeto no serializable)
        print(f"Error de serialización JSON al registrar evento: {e}")
    except Exception as e:
        # Cualquier otro error inesperado
        print(f"Error inesperado al registrar evento: {e}")
    finally:
        if conn:
            conn.close()

# --- PÁGINAS DE LA APLICACIÓN ---

# --- INICIO SECCIÓN DE FEATURES: Votos y Modo Intensivo ---

def cast_vote(conn, username, question_id, vote_type):
    """Registra o actualiza el voto de un usuario y activa la guillotina si es necesario."""
    cursor = conn.cursor()

    # Usamos INSERT OR REPLACE para manejar el UPSERT basado en el índice UNIQUE
    cursor.execute("""
        INSERT OR REPLACE INTO question_votes (user_username, question_id, vote_type, timestamp)
        VALUES (?, ?, ?, ?)
    """, (username, question_id, vote_type, datetime.datetime.now()))

    # --- Lógica del Gatillo (La Guillotina) ---
    if vote_type == -1:
        # Contar los votos negativos para esta pregunta
        unlike_count = cursor.execute(
            "SELECT COUNT(*) FROM question_votes WHERE question_id = ? AND vote_type = -1",
            (question_id,)
        ).fetchone()[0]
        
        # La Regla: Si hay 3 o más 'unlikes', la pregunta necesita revisión
        if unlike_count >= 3:
            cursor.execute("UPDATE questions SET status = 'needs_revision' WHERE id = ?", (question_id,))
            st.toast(f"Pregunta {question_id} enviada a revisión por votos negativos.")

def update_karma(conn, username, question_id, vote_type):
    """
    Gestiona el voto de un usuario y actualiza el contador de karma denormalizado
    en la tabla de preguntas dentro de una única transacción.
    """
    # 1. Registrar el voto individual
    cast_vote(conn, username, question_id, vote_type)
    
    # 2. Recalcular el karma total
    # Contamos los likes (1) y restamos los unlikes (-1)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT SUM(vote_type) FROM question_votes WHERE question_id = ?",
        (question_id,)
    )
    new_karma = cursor.fetchone()[0] or 0
    
    # 3. Actualizar el contador denormalizado en la tabla de preguntas
    cursor.execute(
        "UPDATE questions SET karma = ? WHERE id = ?",
        (new_karma, question_id)
    )

def get_question_votes(question_id):
    """Obtiene el conteo de likes y unlikes para una pregunta."""
    conn = get_db_conn()
    # Usamos COALESCE para asegurar que devolvemos 0 si no hay votos de un tipo
    query = """
        SELECT 
            COALESCE(SUM(CASE WHEN vote_type = 1 THEN 1 ELSE 0 END), 0) as likes,
            COALESCE(SUM(CASE WHEN vote_type = -1 THEN 1 ELSE 0 END), 0) as unlikes
        FROM question_votes
        WHERE question_id = ?
    """
    votes = conn.execute(query, (question_id,)).fetchone()
    conn.close()
    
    return votes['likes'], votes['unlikes']

def has_user_voted(username, question_id):
    """Verifica si un usuario ya ha votado por una pregunta específica."""
    conn = get_db_conn()
    vote = conn.execute(
        "SELECT 1 FROM question_votes WHERE user_username = ? AND question_id = ?",
        (username, question_id)
    ).fetchone()
    conn.close()
    return vote is not None

def calculate_user_score(username, days_limit=3):
    """
    Calcula el puntaje de un usuario en Modo Intensivo bajo un sistema de Ciclos Cerrados con Deuda.
    Retorna: (puntaje_visible, creadas, respondidas, deuda_pendiente)
    """
    conn = get_db_conn()
    
    # 1. Obtener fecha de inicio del desafío
    user = conn.execute("SELECT intensive_start_date FROM users WHERE username = ?", (username,)).fetchone()
    
    # Calculamos el inicio de la ventana deslizante estándar (hace X días)
    window_start = datetime.datetime.now() - datetime.timedelta(days=days_limit)
    
    # Por defecto, filtramos por la ventana deslizante
    start_date_filter = window_start
    debt = 0

    # 2. Lógica de Ciclos Cerrados (Modo Intensivo)
    if user and user['intensive_start_date']:
        start_str = user['intensive_start_date']
        try:
            # 1. Identificamos la fecha base de activación
            try:
                intensive_start = datetime.datetime.strptime(start_str, '%Y-%m-%d')
            except ValueError:
                intensive_start = datetime.datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')

            # 2. Calculamos en qué ciclo estamos (ej: Día 4 con límite de 3 = Ciclo 1)
            days_active = (datetime.datetime.now() - intensive_start).days
            cycle_duration = days_limit if days_limit > 0 else 3
            current_cycle_index = max(0, days_active) // cycle_duration

            # 3. EL ANCLAJE: El filtro siempre empieza al inicio del ciclo actual
            start_of_current_cycle = intensive_start + datetime.timedelta(days=current_cycle_index * cycle_duration)

            # 4. Actualizamos el filtro de la consulta SQL
            start_date_filter = start_of_current_cycle

            # 5. CÁLCULO DE DEUDA (Ciclo Anterior)
            if current_cycle_index > 0:
                start_of_previous_cycle = start_of_current_cycle - datetime.timedelta(days=cycle_duration)
                
                query_prev = """
                    SELECT action_type 
                    FROM activity_log 
                    WHERE username = ? 
                      AND timestamp >= ? AND timestamp < ?
                """
                logs_prev = conn.execute(query_prev, (username, start_of_previous_cycle, start_of_current_cycle)).fetchall()
                
                puntos_ciclo_anterior = 0
                for log in logs_prev:
                    if log['action_type'] in ['answer', 'answer_submitted']: puntos_ciclo_anterior += 1
                    elif log['action_type'] == 'create': puntos_ciclo_anterior += 2
                
                debt = max(0, 30 - puntos_ciclo_anterior)
        except Exception as e:
            print(f"⚠️ Error en cálculo de ciclos intensivos: {e}")

    # 3. Contar puntos (Answer=1, Create=2)
    query = """
        SELECT action_type 
        FROM activity_log 
        WHERE username = ? 
          AND timestamp >= ?
    """
    logs = conn.execute(query, (username, start_date_filter)).fetchall()
    conn.close()

    puntos_ciclo_actual = 0
    num_creadas = 0
    num_respuestas = 0
    for log in logs:
        action = log['action_type']
        if action in ['answer', 'answer_submitted']:
            puntos_ciclo_actual += 1
            num_respuestas += 1
        elif action == 'create':
            puntos_ciclo_actual += 2
            num_creadas += 1
            
    # El score visible para la barra de progreso es el puntaje actual menos la deuda.
    visible_score = max(0, puntos_ciclo_actual - debt)
    return visible_score, num_creadas, num_respuestas, debt

def show_productivity_widget():
    ui.show_productivity_widget()

def get_index_safely(lista, valor):
    """
    Busca de forma segura el índice de un valor en una lista.
    Si no existe o hay un error, retorna 0.
    """
    try:
        return lista.index(valor)
    except (ValueError, AttributeError, TypeError):
        # ValueError: El valor no está en la lista.
        # AttributeError/TypeError: El valor o la lista es None.
        return 0

# --- FIN SECCIÓN DE FEATURES ---


def show_rules_page():
    """Crea una página visual para explicar las reglas, métricas y rangos."""
    st.header("📜 Reglamento y Guía de Supervivencia")
    st.markdown("¡Bienvenido a la arena de conocimiento! Aquí te explicamos cómo funciona todo.")

    tab1, tab2, tab3 = st.tabs(["📊 El Tablero de Control (Métricas)", "🔥 La Constitución del Modo Intensivo", "🏆 Rangos y Medallas"])

    # --- Pestaña 1: Métricas ---
    with tab1:
        st.subheader("📊 El Tablero de Control (Métricas)")
        
        st.markdown("""
        #### Tasa de Aprendizaje
        Esta métrica es clave. Mide tu **conocimiento a largo plazo**. Se calcula sobre las preguntas que has respondido correctamente y cuyo próximo repaso está programado para **más de 7 días** en el futuro. Un porcentaje alto aquí significa que estás reteniendo la información de verdad.
        """)
        
        # Gráfico de Torta para Tasa de Aprendizaje
        df_aprendizaje = pd.DataFrame({
            'Estado': ['Aprendido (Largo Plazo)', 'Por Aprender'],
            'Cantidad': [20, 80]
        })
        chart_aprendizaje = alt.Chart(df_aprendizaje).mark_arc(innerRadius=50).encode(
            theta=alt.Theta(field="Cantidad", type="quantitative"),
            color=alt.Color(field="Estado", type="nominal", scale=alt.Scale(scheme='greens')),
            tooltip=['Estado', 'Cantidad']
        ).properties(
            title='Ej: Tasa de Aprendizaje del 20%'
        )
        st.altair_chart(chart_aprendizaje, use_container_width=True)

        st.markdown("""
        ---
        #### Precisión
        Mide la **calidad de tus respuestas inmediatas**. Es la simple pero poderosa relación entre tus aciertos y tus fallos. Una precisión alta indica que entiendes los conceptos al momento de estudiarlos.
        
        ---
        #### Progreso vs. Experto
        Esto no es solo una carrera contra ti mismo, es una **competencia contra el estándar de los mejores**. Tu progreso se compara con el rendimiento promedio de los **Residentes 🎓**, los usuarios más experimentados que ya han aprobado el examen real. ¡Aspira a superar su marca!
        """)

    # --- Pestaña 2: Modo Intensivo ---
    with tab2:
        st.subheader("🔥 Cómo sobrevivir a la guillotina")
        st.error("**La Regla de Oro:** Debes sumar **30 Puntos** en cada ciclo (normalmente 3 días). Si no cumples, tu cuenta será marcada para eliminación.")

        st.markdown("#### Tabla de Puntuación:")
        st.markdown("""
        | Acción | Puntos | Descripción |
        |---|---|---|
        | 📝 **Crear Pregunta** | **2 Puntos** | El mayor valor. Aportar conocimiento a la comunidad es la acción más recompensada. |
        | 🧠 **Responder Pregunta**| **1 Punto** | Estudiar y contestar preguntas del sistema te mantiene en forma y suma puntos. |
        """)
        
        st.markdown("---")
        st.subheader("Ejemplos de Estrategias de Supervivencia")

        # Gráfico de Barras para Estrategias
        df_estrategias = pd.DataFrame({
            'Estrategia': ['Solo Responder', 'Solo Crear', 'Mix Equilibrado'],
            'Acciones Necesarias': [30, 15, 20], # 30 respuestas, 15 creadas, 10 creadas + 10 respondidas = 20 acciones
            'Detalle': ['30 Respuestas', '15 Preguntas Creadas', '10 Creadas + 10 Respuestas']
        })
        
        chart_estrategias = alt.Chart(df_estrategias).mark_bar().encode(
            x=alt.X('Estrategia', sort=None, title=''),
            y=alt.Y('Acciones Necesarias', title='Cantidad de Acciones para llegar a 30 Pts'),
            color=alt.Color('Estrategia', legend=None),
            tooltip=['Estrategia', 'Detalle']
        ).properties(
            title='Cómo Acumular 30 Puntos'
        )
        st.altair_chart(chart_estrategias, use_container_width=True)
        st.caption("El gráfico muestra cuántas acciones de cada tipo necesitas para cumplir la cuota. Un 'Mix' es a menudo la estrategia más sostenible.")

    # --- Pestaña 3: Rangos y Medallas ---
    with tab3:
        st.subheader("🏆 Jerarquía de la Comunidad")
        
        st.markdown("""
        Tu rango refleja tu pericia, consistencia y contribución.
        
        # 🎓 Residente
        El 'Sensei'. Un usuario que **ha aprobado el examen real** y cuya cuenta ha sido verificada por un administrador. Son la fuente de sabiduría y el estándar a seguir.
        
        # ⭐ Experto
        El 'Alumno Estrella'. Un usuario con una **Precisión superior al 95%** y un volumen de estudio muy alto. Demuestra un dominio casi total del material.
        
        # 🦁 Avanzado
        El pilar de la comunidad. Un usuario **constante y con buen rendimiento**. Sigue las reglas y progresa adecuadamente.
        
        # 🚑 En Riesgo
        Una señal de alerta. Este usuario tiene una **precisión muy baja** o parece estar haciendo 'spam' (responde mucho pero no retiene, indicando falta de aprendizaje real). Necesita mejorar para no ser purgado.
        
        ---
        #### El Poder del Karma (Votos)
        En cada pregunta que respondas, podrás votar si es de buena calidad (👍) o si tiene errores (👎).
        - **Votos Positivos (👍):** Aumentan la reputación de la pregunta y de su creador.
        - **Votos Negativos (👎):** ¡Cuidado! Si una pregunta acumula 3 o más votos negativos, es marcada para revisión por un administrador. Abusar de preguntas de baja calidad puede afectar tu estatus.
        """)

def check_rate_limit():
    """Previene abuso por acciones demasiado rápidas (spam/scraping)."""
    now = datetime.datetime.now()
    last_action = st.session_state.get("last_action_time", None)

    if last_action and (now - last_action).total_seconds() < 2:
        st.warning("⏳ Vas muy rápido. Tómate un respiro.")
        st.stop()
    
    # Actualiza el tiempo de la acción actual para la próxima verificación.
    st.session_state.last_action_time = now


def update_user_activity(conn, username):
    """
    Actualiza la racha y los días de actividad de un usuario de forma segura,
    utilizando una conexión de BD existente.
    """
    # Aseguramos traer todas las columnas necesarias para la lógica
    user = conn.execute("SELECT last_active_date, current_streak, total_active_days, last_streak_date FROM users WHERE username = ?", (username,)).fetchone()
    
    if not user:
        return

    today = datetime.date.today()
    last_active_str = user['last_active_date']
    
    # Si el usuario ya estudió hoy, no hacemos nada.
    if last_active_str == today.strftime('%Y-%m-%d'):
        return
        
    current_streak = user['current_streak'] or 0
    total_active_days = user['total_active_days'] or 0

    if last_active_str is None:
        # Primer día de actividad
        new_streak = 1
        new_total_days = 1
    else:
        last_active_date = datetime.datetime.strptime(last_active_str, '%Y-%m-%d').date()
        yesterday = today - datetime.timedelta(days=1)
        
        if last_active_date == yesterday:
            # La racha continúa
            new_streak = current_streak + 1
            new_total_days = total_active_days + 1
        else:
            # La racha se rompió
            new_streak = 1
            new_total_days = total_active_days + 1
            
    conn.execute(
        "UPDATE users SET last_active_date = ?, current_streak = ?, total_active_days = ? WHERE username = ?",
        (today, new_streak, new_total_days, username)
    )

def show_login_page():
    """Muestra un dashboard de bienvenida con métricas y gestiona el login/registro."""
    # --- 1. SECCIÓN MOTIVACIONAL Y MÉTRICAS ---
    # Conexión solo para métricas
    conn_metrics = get_db_conn()
    try:
        q_count = conn_metrics.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        u_count = conn_metrics.execute("SELECT COUNT(*) FROM users WHERE role != 'admin' AND status = 'active'").fetchone()[0]
        try:
            del_count = conn_metrics.execute("SELECT COUNT(*) FROM deleted_users_log").fetchone()[0]
        except sqlite3.OperationalError:
            del_count = 0 # Fallback si la tabla no existe
    except Exception as e:
        q_count, u_count, del_count = "N/A", "N/A", "N/A"
        print(f"DEBUG: Error cargando métricas del login: {e}")
    finally:
        if conn_metrics:
            conn_metrics.close()

    # Frase Central
    st.markdown("""
        <div style='text-align: center; padding: 20px 0;'>
            <h2 style='font-size: 24px; font-weight: 600; color: #E0E0E0;'>
                "La única diferencia entre el que se queja y el que mejora es que el segundo no se rinde."
            </h2>
            <hr style='margin-top: 20px; margin-bottom: 20px; border-color: #333;'>
        </div>
    """, unsafe_allow_html=True)

    # Métricas Sociales
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        st.metric("📚 Preguntas en Banco", f"{q_count}")
    with col2:
        st.metric("👥 Estudiantes Activos", f"{u_count}")
    with col3:
        st.markdown(f"<br><p style='font-size: 12px; color: #666; text-align: center;'>☠️ {del_count} Estudiantes Eliminados</p>", unsafe_allow_html=True)
    
    st.markdown("---")

    # --- 2. LOGIN (Mantenemos la lógica existente pero limpia) ---
    with st.form("login_form"):
        st.markdown("### Ingreso")
        username = st.text_input("Nombre de usuario")
        password = st.text_input("Contraseña", type="password")
        login_submitted = st.form_submit_button("Ingresar")

        if login_submitted:
            check_rate_limit()
            # Higiene de datos: eliminar espacios y forzar minúsculas
            clean_username = username.strip().lower()
            conn = get_db_conn()
            
            # --- INICIO: Lógica Anti-Fuerza Bruta ---
            user = conn.execute("SELECT * FROM users WHERE username = ?", (clean_username,)).fetchone()

            if not user:
                st.error("Usuario o contraseña incorrectos.")
                if conn: conn.close()
                return

            # 1. Chequeo de Bloqueo
            if user['lockout_until']:
                try:
                    lockout_time = datetime.datetime.fromisoformat(user['lockout_until'])
                    if lockout_time > datetime.datetime.now():
                        remaining_time = lockout_time - datetime.datetime.now()
                        minutes = math.ceil(remaining_time.total_seconds() / 60)
                        st.error(f"Cuenta bloqueada temporalmente. Intenta de nuevo en {minutes} minutos.")
                        if conn: conn.close()
                        return
                except (ValueError, TypeError):
                    # Ignorar si el formato de fecha es inválido y proceder
                    pass

            # 2. Verificación de Contraseña
            if verify_password(password, user['password_hash']):
                # ACIERTO: Resetear contadores y proceder al login
                if user['failed_attempts'] > 0 or user['lockout_until'] is not None:
                    conn.execute("UPDATE users SET failed_attempts = 0, lockout_until = NULL WHERE username = ?", (clean_username,))
                    conn.commit()
                
                # --- Lógica de login existente ---
                if user['status'] == 'pending_delete':
                    st.error("Cuenta bloqueada por incumplimiento. Contacta al administrador.")
                    conn.close()
                    return

                # 3. Verificación de Expiración (Suscripción)
                if user['access_expiration']:
                    try:
                        exp_date = datetime.datetime.strptime(user['access_expiration'], '%Y-%m-%d').date()
                        if datetime.date.today() > exp_date:
                            st.error(f"🚫 Tu acceso expiró el {user['access_expiration']}. Por favor renueva tu suscripción.")
                            if conn: conn.close()
                            return
                    except ValueError:
                        pass # Fecha inválida o nula, permitimos acceso (o logueamos error)

                if user['is_intensive']:
                    is_in_grace_period = False
                    start_date_str = user['intensive_start_date']

                    if start_date_str is None:
                        today = datetime.date.today()
                        conn.execute("UPDATE users SET intensive_start_date = ? WHERE username = ?", (today, clean_username))
                        conn.commit()
                        st.success(f"🛡️ Periodo de Gracia activado. Tienes {user['max_inactivity_days']} días para cumplir tu cuota.")
                        is_in_grace_period = True
                    else:
                        start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                        days_active = (datetime.date.today() - start_date).days
                        if days_active < user['max_inactivity_days']:
                            is_in_grace_period = True

                    if not is_in_grace_period:
                        score, _, _ = calculate_user_score(clean_username, user['max_inactivity_days'])
                        last_activity_row = conn.execute("SELECT MAX(timestamp) as last_ts FROM activity_log WHERE username = ?", (clean_username,)).fetchone()
                        is_inactive = False
                        if last_activity_row and last_activity_row['last_ts']:
                            last_activity_date = datetime.datetime.fromisoformat(last_activity_row['last_ts'])
                            if (datetime.datetime.now() - last_activity_date).days > user['max_inactivity_days']:
                                is_inactive = True
                        else:
                            is_inactive = True
                        if score < 30 or is_inactive:
                            conn.execute("UPDATE users SET status = 'pending_delete' WHERE username = ?", (clean_username,))
                            conn.commit()
                            st.error("Cuenta bloqueada por incumplimiento del Modo Intensivo. Contacta al administrador.")
                            conn.close()
                            return
                
                if user['is_approved'] == 1:
                    st.session_state.logged_in = True
                    st.session_state.current_user = user['username']
                    st.session_state.user_role = user['role']
                    st.session_state.current_page = "evaluacion"
                    conn.close()
                    st.rerun()
                else:
                    st.error("Tu cuenta está registrada, pero aún no ha sido aprobada por un administrador.")
            else:
                # FALLO: Incrementar contador y potencialmente bloquear
                new_attempts = user['failed_attempts'] + 1
                if new_attempts >= 5:
                    lockout_time = datetime.datetime.now() + datetime.timedelta(minutes=15)
                    conn.execute("UPDATE users SET failed_attempts = 0, lockout_until = ? WHERE username = ?", (lockout_time.isoformat(), clean_username))
                    st.error("Contraseña incorrecta. Has superado el límite de intentos. Cuenta bloqueada por 15 minutos.")
                else:
                    conn.execute("UPDATE users SET failed_attempts = ? WHERE username = ?", (new_attempts, clean_username))
                    st.error(f"Usuario o contraseña incorrectos. Intento {new_attempts} de 5.")
                
                conn.commit()
            
            if conn:
                conn.close()

    # --- 3. REGISTRO (ENCAPSULADO) ---
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("📝 Registro de Usuario Nuevo", expanded=False):
        with st.form("register_form", clear_on_submit=True):
            new_username = st.text_input("Nuevo nombre de usuario")
            new_password = st.text_input("Nueva contraseña", type="password")
            reg_submitted = st.form_submit_button("Registrarse")

            if reg_submitted:
                # Higiene de datos: guardar siempre limpio
                clean_new_username = new_username.strip().lower()

                if not clean_new_username or not new_password:
                    st.warning("Usuario y contraseña no pueden estar vacíos.")
                elif clean_new_username == st.secrets["ADMIN_USER"].lower():
                     st.error("Nombre de usuario no disponible.")
                else:
                    try:
                        password_new_bytes = new_password.encode('utf-8')[:72]
                        hashed_pass = pwd_context.hash(password_new_bytes)
                        conn = get_db_conn()
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
                            (clean_new_username, hashed_pass)
                        )
                        conn.commit()
                        conn.close()
                        st.success("¡Usuario registrado! Tu cuenta está pendiente de aprobación por un administrador.")
                    except sqlite3.IntegrityError:
                        st.error("Ese nombre de usuario ya existe.")
                    except Exception as e:
                        st.error(f"Error al registrar: {e}")

def show_create_page():
    """Muestra el formulario para crear nuevas preguntas (con etiquetas)."""
    st.subheader("🖊️ Crear Nueva Pregunta")
    
    with st.form("create_question_form", clear_on_submit=True):
        enunciado = st.text_area("Enunciado de la pregunta")
        opciones = []
        opciones.append(st.text_input("Opción A"))
        opciones.append(st.text_input("Opción B"))
        opciones.append(st.text_input("Opción C"))
        opciones.append(st.text_input("Opción D"))
        
        correcta_idx = st.radio("Respuesta Correcta", (0, 1, 2, 3), format_func=lambda x: f"Opción {chr(65+x)}")
        retroalimentacion = st.text_area("Retroalimentación (Explicación)")
        
        st.markdown("---")
        tag_categoria = st.selectbox("Etiqueta 1: Categoría", options=get_all_categories(), index=None)
        tag_tema = st.text_input("Etiqueta 2: Tema")
        
        submitted = st.form_submit_button("Guardar Pregunta")
        
        if submitted:
            check_rate_limit()
            if not all([enunciado, opciones[0], opciones[1], opciones[2], opciones[3], retroalimentacion, tag_categoria, tag_tema]):
                st.warning("Por favor, completa todos los campos.")
            else:
                conn = get_db_conn()
                cursor = conn.cursor()
                opciones_str = "|".join(opciones) 
                correcta = opciones[correcta_idx]
                owner = st.session_state.current_user
                
                cursor.execute(
                    "INSERT INTO questions (owner_username, enunciado, opciones, correcta, retroalimentacion, tag_categoria, tag_tema) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (owner, enunciado, opciones_str, correcta, retroalimentacion, tag_categoria, tag_tema)
                )

                # --- INICIO SECCIÓN MODO INTENSIVO: Registrar actividad ---
                cursor.execute(
                    "INSERT INTO activity_log (username, action_type, timestamp) VALUES (?, 'create', ?)",
                    (owner, datetime.datetime.now())
                )
                # --- FIN SECCIÓN MODO INTENSIVO ---
                
                # --- INICIO ACTUALIZACIÓN DE RACHA ---
                update_user_activity(conn, owner)
                # --- FIN ACTUALIZACIÓN DE RACHA ---

                conn.commit()
                conn.close()
                st.success("¡Pregunta guardada con éxito!")

# FSRS Engine logic moved to fsrs_engine.py

# DEFINICIÓN MAESTRA DEL GOLDEN RATIO (Sin tildes para compatibilidad)
# GOLDEN_RATIO_DETAILED moved to fsrs_engine.py

# Wrapper eliminado: get_next_question_for_user ahora se importa directamente de fsrs_engine


def update_srs(conn, username, question_id, difficulty_rating):
    """
    Actualiza el SRS en la BD usando la lógica FSRS v4 simplificada y registra la actividad.
    Rating mapping: 'difícil'->1 (Olvido), 'medio'->3 (Costoso), 'fácil'->5 (Bien).
    """
    cursor = conn.cursor()
    today = datetime.date.today()

    # 1. Mapeo del rating de entrada a un grado numérico
    if difficulty_rating == "difícil":
        grade = 1
    elif difficulty_rating == "medio":
        grade = 3
    else: # "fácil"
        grade = 5
    
    # 2. Obtener el estado SRS actual de la pregunta para el usuario
    cursor.execute(
        "SELECT stability, difficulty FROM progress WHERE username = ? AND question_id = ?",
        (username, question_id)
    )
    progress = cursor.fetchone()

    s_prev = progress['stability'] if progress and progress['stability'] is not None else 0.0
    d_prev = progress['difficulty'] if progress and progress['difficulty'] is not None else 0.0

    # 3. Cálculo de Dificultad (D)
    if d_prev == 0.0:
        d_new = 5.0  # Valor inicial si es la primera vez
    else:
        # El 'costo' en la fórmula se deriva del 'grade'
        d_new = d_prev - 0.32 + (0.18 * (grade - 3.0))
    
    d_new = max(1.0, min(10.0, d_new))  # Se asegura que D esté entre 1.0 y 10.0

    # 4. Cálculo de Estabilidad (S)
    if s_prev == 0.0:  # Si la tarjeta es nueva
        if grade == 1: s_new = 0.4
        elif grade == 3: s_new = 2.0
        else: s_new = 5.0
    else:  # Si la tarjeta está en repaso
        if grade == 1:  # Olvido
            s_new = s_prev * 0.4  # Penalización a la estabilidad
        else:  # Recordado (Medio o Fácil)
            factor_crecimiento = 1 + (1.5 / (d_new * 0.3))
            s_new = s_prev * factor_crecimiento

    # 5. Cálculo del nuevo Intervalo (I)
    # El intervalo busca una probabilidad de recuerdo (retrievability) del 90%
    new_interval = max(1, round(s_new * 0.9))

    # 6. Cálculo de la nueva fecha de repaso
    new_due_date = today + datetime.timedelta(days=int(new_interval))

    # 7. Actualización de contadores de aciertos/fallos (lógica heredada)
    cursor.execute("SELECT aciertos, fallos FROM progress WHERE username = ? AND question_id = ?", (username, question_id))
    aciertos_fallos = cursor.fetchone()
    aciertos = aciertos_fallos['aciertos'] if aciertos_fallos else 0
    fallos = aciertos_fallos['fallos'] if aciertos_fallos else 0
    
    if grade == 1:
        fallos += 1
    else:
        aciertos += 1

    # 8. Actualizar la base de datos con todos los nuevos valores (UPSERT)
    cursor.execute("""
        INSERT INTO progress (username, question_id, due_date, interval, aciertos, fallos, stability, difficulty, last_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(username, question_id) DO UPDATE SET
            due_date = excluded.due_date,
            interval = excluded.interval,
            aciertos = excluded.aciertos,
            fallos = excluded.fallos,
            stability = excluded.stability,
            difficulty = excluded.difficulty,
            last_review = excluded.last_review
    """, (username, question_id, new_due_date, new_interval, aciertos, fallos, s_new, d_new, today))
    
    # --- Registrar actividad para Modo Intensivo y Rachas ---
    cursor.execute(
        "INSERT INTO activity_log (username, action_type, timestamp) VALUES (?, 'answer', ?)",
        (username, datetime.datetime.now())
    )
    update_user_activity(conn, username)

def reset_evaluation_state():
    """Resetea el estado para mostrar la siguiente pregunta."""
    st.session_state.eval_state = "showing_question"
    st.session_state.user_answer = None
    if 'current_eval_question_data' in st.session_state:
        del st.session_state['current_eval_question_data']
    # También es buena idea limpiar el estado de avance previo al resetear
    if 'previous_is_advance' in st.session_state:
        del st.session_state['previous_is_advance']

def clear_evaluation_memory():
    """
    Limpia TODAS las claves de sesión relacionadas con una evaluación activa.
    Esencial para evitar contaminación de datos entre sesiones de diferentes usuarios.
    """
    # Lista de prefijos y claves exactas que deben morir al cambiar de usuario
    keys_to_reset = [
        'card_state_', 'user_answer_', 'shuffled_options_', 'timer_start_', # Prefijos de render_question_card
        'current_eval_question_data', 'previous_is_advance', # Estado de la cola de evaluación
        'topic_question_id', # Estado de la biblioteca
        'duel_state', 'current_duel_id', 'duel_question_index', 'duel_user_score', # Estado de duelos
        'duel_history', 'duel_questions', 'duel_question_start_time',
        'eval_state', 'user_answer', # Claves de la función de reset vieja
        'last_action_time' # Rate limiter
    ]
    
    # Usamos list(st.session_state.keys()) para crear una copia, ya que no se puede
    # modificar un diccionario mientras se itera sobre él.
    for key in list(st.session_state.keys()):
        # Comprobamos si la clave comienza con alguno de los prefijos o es una clave exacta
        if any(key.startswith(prefix) for prefix in keys_to_reset) or key in keys_to_reset:
            del st.session_state[key]

def render_question_card(question_id):
    # --- SENSOR DE INICIO (CRONÓMETRO) ---
    # Usamos el ID de la pregunta para crear un timer único
    start_key = f"timer_start_{question_id}"
    if start_key not in st.session_state:
        st.session_state[start_key] = datetime.datetime.now()
    
    # --- LÓGICA AUTO-CURABLE (ANTI-ZOMBIE) ---
    # Detectamos si la tarjeta cree que ya terminó ('done') pero se le ha pedido renderizar de nuevo.
    key_state = f"card_state_{question_id}"
    current_state = st.session_state.get(key_state)

    # Si el estado es 'done', significa que es un residuo de una sesión anterior.
    # Debemos reiniciarlo obligatoriamente para que el usuario pueda responder.
    if current_state == 'done':
        # Borramos variables clave para forzar un reinicio limpio
        keys_to_purge = [
            key_state,
            f"user_answer_{question_id}",
            f"feedback_shown_{question_id}",
            f"shuffled_options_{question_id}"
        ]
        for k in keys_to_purge:
            if k in st.session_state:
                del st.session_state[k]
        
        # Forzamos el estado inicial
        st.session_state[key_state] = "showing_question"
    # ----------------------------------------
    # --- 1. Inicialización y Carga de Datos ---
    next_question_requested = False
    card_state_key = f"card_state_{question_id}"
    user_answer_key = f"user_answer_{question_id}"
    
    # Inicializar el estado de la tarjeta si no existe
    if card_state_key not in st.session_state:
        st.session_state[card_state_key] = "showing_question"

    conn = get_db_conn()
    pregunta_row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    conn.close()
    
    if not pregunta_row:
        st.error("Error: La pregunta no se encontró en la base de datos.")
        return True # Solicitar pasar a la siguiente para evitar un bucle

    pregunta = dict(pregunta_row)

    # --- BLINDAJE CONTRA DATOS CORRUPTOS ---
    try:
        # Asegurarse de que el campo 'opciones' no es None, no está vacío y contiene el separador.
        if not pregunta['opciones'] or '|' not in pregunta['opciones']:
            raise ValueError("Formato de opciones inválido o ausente.")
        
        parsed_options = pregunta['opciones'].split('|')
        
        # Validar que después del split, no haya quedado una lista de strings vacíos
        if len(parsed_options) < 2 or not all(op.strip() for op in parsed_options):
             raise ValueError("Al menos una de las opciones está vacía.")
             
    except (ValueError, TypeError, KeyError) as e:
        st.error(f"Datos de pregunta corruptos (ID: {question_id}). Un administrador debería revisarla. Saltando pregunta.")
        st.caption(f"Detalle técnico: {e}")
        return True # Devuelve True para que show_evaluation_page pida la siguiente.

    # --- LÓGICA DE ANCLAJE INTELIGENTE (MODIFICADO) ---
    
    # 1. Consultar preferencia del Admin
    conn_pref = get_db_conn()
    try:
        pref_row = conn_pref.execute("SELECT value FROM system_config WHERE key = 'show_option_prefixes'").fetchone()
        show_prefixes = (pref_row['value'] == "True") if pref_row else True
    finally:
        conn_pref.close()

    # 2. Construir opciones según la preferencia
    if show_prefixes:
        # Modo Clásico: Limpia IA y agrega [A], [B]...
        original_options_with_prefix = [
            f"**[{chr(65+i)}]** {clean_ai_prefixes(op)}" 
            for i, op in enumerate(parsed_options)
        ]
    else:
        # Modo Limpio: Solo texto (Limpia IA y NO agrega nada)
        original_options_with_prefix = [
            clean_ai_prefixes(op) 
            for op in parsed_options
        ]
    
    # Identificar la respuesta correcta con su nuevo prefijo
    correct_option_original_text = pregunta['correcta'].strip()
    correct_option_with_prefix = ""
    for i, op in enumerate(parsed_options):
        if op.strip() == correct_option_original_text:
            correct_option_with_prefix = original_options_with_prefix[i]
            break

    # 2. Mezcla (Shuffle) Persistente
    shuffle_key = f"shuffled_options_{question_id}"
    if shuffle_key in st.session_state:
        display_options = st.session_state[shuffle_key]
    else:
        display_options = original_options_with_prefix.copy()
        random.shuffle(display_options)
        st.session_state[shuffle_key] = display_options
    # -------------------------------------

    # --- 2. Lógica de Renderizado y Estado ---
    # --- PARCHE ANTI-MODO LECTOR ---
    st.markdown("""
        <style>
        /* Inyectar CSS para confundir al algoritmo del Modo Lector de Safari */
        .stMarkdown div p {
            -webkit-user-modify: read-write-plaintext-only;
        }
        </style>
        """, unsafe_allow_html=True)

    # Envolver el texto de la pregunta en un contenedor identificado como 'interactivo'
    st.markdown(f"""
    <div class='notranslate' style='line-height: 1.6; letter-spacing: 0.1px; font-size: 1.25rem; font-weight: 600; margin-bottom: 1rem;'>
        {pregunta['enunciado']}
    </div>
    """, unsafe_allow_html=True)

    # --- ESTADO: MOSTRANDO PREGUNTA Y OPCIONES ---
    if st.session_state.get(card_state_key) == "showing_question":
        with st.form(f"form_{question_id}"):
            user_choice = st.radio(
                "Selecciona tu respuesta:", 
                options=display_options,
                key=f"radio_{question_id}"
            )
            if st.form_submit_button("Responder"):
                # --- NUEVO: TRIGGER DE CONSUMO (Modo Invitado) ---
                if st.session_state.get('user_role') == 'guest':
                    increment_guest_usage()

                # --- AUDITORIA: Actualizar estadísticas de sesión en tiempo real ---
                if st.session_state.get('practice_mode'):
                    if 'session_stats' not in st.session_state: st.session_state.session_stats = {'correct': 0, 'total': 0}
                    st.session_state.session_stats['total'] += 1
                    if user_choice == correct_option_with_prefix: st.session_state.session_stats['correct'] += 1
                # ------------------------------------------------------------------

                # --- AUDITORÍA DE DATOS: Validación y Registro de Perfil ---
                is_correct = (user_choice == correct_option_with_prefix)

                # Crear una etiqueta de perfil dinámica
                if st.session_state.get('user_role') == 'guest':
                    paso_examen = st.session_state.get('guest_profile_passed', False)
                    perfil_mineria = "Guest_Ganador_Previo" if paso_examen else "Guest_Aspirante_Puro"
                else:
                    perfil_mineria = st.session_state.current_user

                # Actualizar el log_event
                log_event(perfil_mineria, 'answer_submitted', {
                    'question_id': question_id,
                    'result': 'correct' if is_correct else 'incorrect',
                    'difficulty': pregunta.get('difficulty_level', 'Media'),
                    'topic': pregunta.get('tag_tema', 'General')
                })
                # -----------------------------------------------------------

                # --- MINERÍA DE DATOS (FASE 3) ---
                log_mining_data(question_id, is_correct)
                # ---------------------------------

                st.session_state[user_answer_key] = user_choice
                st.session_state[card_state_key] = "showing_feedback"
                st.rerun()

    # --- ESTADO: MOSTRANDO FEEDBACK, KARMA Y SRS ---
    elif st.session_state.get(card_state_key) == "showing_feedback":
        # --- AUDITORÍA: Recuperar perfil para logs en feedback ---
        conn_audit = get_db_conn()
        try:
            user_audit = conn_audit.execute("SELECT is_resident FROM users WHERE username = ?", (st.session_state.current_user,)).fetchone()
            is_resident_audit = user_audit['is_resident'] if user_audit else 0
        finally:
            conn_audit.close()
        
        respuesta_usuario = st.session_state.get(user_answer_key)
        
        # Mostrar opciones con feedback visual
        for op in display_options:
            if op == correct_option_with_prefix:
                st.success(f"**{op} (Correcta)**")
            elif op == respuesta_usuario:
                st.error(f"**{op} (Tu respuesta)**")
            else:
                st.write(op)
        
        st.info(f"**Retroalimentación:**\n{pregunta['retroalimentacion']}")
        st.markdown("---")

        # --- Sub-componente: Botones de Karma ---
        col_karma, col_srs = st.columns([1, 2])
        with col_karma:
            st.write("**Calidad:**")
            
            user_has_voted = has_user_voted(st.session_state.current_user, question_id)
            
            if user_has_voted:
                st.caption("✅ Ya has votado.")
            else:
                k_col1, k_col2 = st.columns(2)
                
                def handle_karma_update(vote_type):
                    conn = None
                    try:
                        conn = get_db_conn()
                        update_karma(conn, st.session_state.current_user, question_id, vote_type)
                        conn.commit()
                    finally:
                        if conn: conn.close()
                    st.rerun()

                if k_col1.button(f"👍 {pregunta['karma']}", key=f"karma_up_{question_id}"):
                    handle_karma_update(1)
                if k_col2.button("👎", key=f"karma_down_{question_id}"):
                    handle_karma_update(-1)

        # --- Sub-componente: Botones SRS ---
        with col_srs:
            # 1. Detectar Privilegios (Modo o Intensivo)
            conn_strat = get_db_conn()
            try:
                strat_row = conn_strat.execute("SELECT strategy_mode, is_intensive FROM users WHERE username = ?", (st.session_state.current_user,)).fetchone()
                current_strat = strat_row['strategy_mode'] if strat_row else 'STANDARD'
                user_is_intensive = bool(strat_row['is_intensive']) if (strat_row and strat_row['is_intensive']) else False
            except Exception:
                current_strat = 'STANDARD'
                user_is_intensive = False
            finally:
                conn_strat.close()

            # Condición de visualización
            show_fsrs_buttons = (current_strat == 'MAFU') or user_is_intensive

            # 2. Lógica Condicional de UI
            if show_fsrs_buttons:
                st.write("**Calificación FSRS:**")
                fsrs_cols = st.columns(4)
                
                def handle_fsrs_vote(rating):
                    # Instancia del motor FSRS v5
                    engine = FSRS_v5_Engine()
                    
                    conn = get_db_conn()
                    try:
                        # Recuperar datos actuales
                        row = conn.execute("SELECT stability, difficulty, aciertos, fallos, last_review FROM progress WHERE username = ? AND question_id = ?", (st.session_state.current_user, question_id)).fetchone()
                        
                        current_s = row['stability'] if row and row['stability'] else 0.0
                        current_d = row['difficulty'] if row and row['difficulty'] else 0.0
                        aciertos = row['aciertos'] if row else 0
                        fallos = row['fallos'] if row else 0
                        current_reps = aciertos + fallos
                        last_review_str = row['last_review'] if row else None
                        
                        today = datetime.date.today()
                        days_elapsed = 0
                        if last_review_str:
                            try:
                                last_date = datetime.datetime.strptime(last_review_str, '%Y-%m-%d').date()
                                days_elapsed = (today - last_date).days
                            except:
                                days_elapsed = 0
                        
                        # Calcular nuevos valores
                        new_s, new_d = engine.calculate_next_review(current_s, current_d, current_reps, rating, days_elapsed)
                        ivl = engine.get_next_interval_days(new_s)
                        new_due_date = today + datetime.timedelta(days=ivl)
                        
                        # Actualizar contadores
                        new_aciertos = aciertos + (1 if rating > 1 else 0)
                        new_fallos = fallos + (1 if rating == 1 else 0)
                        
                        # Actualizar DB
                        conn.execute("""
                            INSERT INTO progress (username, question_id, due_date, interval, aciertos, fallos, stability, difficulty, last_review)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(username, question_id) DO UPDATE SET
                                due_date = excluded.due_date,
                                interval = excluded.interval,
                                aciertos = excluded.aciertos,
                                fallos = excluded.fallos,
                                stability = excluded.stability,
                                difficulty = excluded.difficulty,
                                last_review = excluded.last_review
                        """, (st.session_state.current_user, question_id, new_due_date, ivl, new_aciertos, new_fallos, new_s, new_d, today))
                        
                        # Registrar actividad
                        update_user_activity(conn, st.session_state.current_user)
                        
                        # --- AUDITORIA: Registrar ID respondido para evitar repetición ---
                        if st.session_state.get('practice_mode'):
                            if 'session_answered_ids' not in st.session_state: st.session_state.session_answered_ids = []
                            st.session_state.session_answered_ids.append(question_id)
                        
                        # --- AUDITORIA: Limpieza profunda de estado (Anti-Ghosting) ---
                        keys_to_nuke = [f"card_state_{question_id}", f"user_answer_{question_id}", f"shuffled_options_{question_id}"]
                        for k in keys_to_nuke:
                            if k in st.session_state: del st.session_state[k]
                        
                        conn.commit()
                        
                    finally:
                        conn.close()
                    
                    # Limpiar estado y recargar para siguiente pregunta
                    if 'current_eval_question_data' in st.session_state:
                        st.session_state.previous_is_advance = st.session_state.current_eval_question_data.get('is_advance', False)
                        del st.session_state.current_eval_question_data
                    
                    # FIX: Limpiar también el estado de la biblioteca por temas para evitar bucle infinito
                    if 'topic_question_id' in st.session_state:
                        del st.session_state.topic_question_id
                    st.rerun()

                # Botones FSRS
                if fsrs_cols[0].button("Olvido (1)", key=f"fsrs_1_{question_id}", type="primary", help="Rojo - Reiniciar"):
                    handle_fsrs_vote(1)
                if fsrs_cols[1].button("Difícil (2)", key=f"fsrs_2_{question_id}", help="Naranja - Costoso"):
                    handle_fsrs_vote(2)
                if fsrs_cols[2].button("Bien (3)", key=f"fsrs_3_{question_id}", help="Azul - Correcto"):
                    handle_fsrs_vote(3)
                if fsrs_cols[3].button("Fácil (4)", key=f"fsrs_4_{question_id}", help="Verde - Muy fácil"):
                    handle_fsrs_vote(4)

            else:
                # Caso Usuario 'STANDARD' (Lógica Original)
                st.write("**¿Qué tan difícil fue?**")
                srs_cols = st.columns(3)
            
                def handle_srs_update(difficulty):
                    check_rate_limit()
                    # ... (Lógica de log existente se mantiene implícita en update_srs o se puede duplicar si es necesario, 
                    # pero aquí mantenemos la llamada original para no romper el flujo standard)
                    conn = get_db_conn()
                    
                    # --- AUDITORIA: Registrar ID respondido para evitar repetición ---
                    if st.session_state.get('practice_mode'):
                        if 'session_answered_ids' not in st.session_state: st.session_state.session_answered_ids = []
                        st.session_state.session_answered_ids.append(question_id)
                    
                    # --- AUDITORIA: Limpieza profunda de estado (Anti-Ghosting) ---
                    keys_to_nuke = [f"card_state_{question_id}", f"user_answer_{question_id}", f"shuffled_options_{question_id}"]
                    for k in keys_to_nuke:
                        if k in st.session_state: del st.session_state[k]

                    update_srs(conn, st.session_state.current_user, question_id, difficulty)
                    conn.commit()
                    conn.close()
                    st.session_state[card_state_key] = "done"
                    
                if srs_cols[0].button("Difícil", key=f"srs_hard_{question_id}"):
                    log_event(st.session_state.current_user, 'difficulty_rated', {
                        'question_id': question_id,
                        'user_rating': 'Difícil',
                        'is_resident': is_resident_audit
                    })
                    handle_srs_update("difícil")
                    next_question_requested = True
                if srs_cols[1].button("Medio", key=f"srs_mid_{question_id}"):
                    log_event(st.session_state.current_user, 'difficulty_rated', {
                        'question_id': question_id,
                        'user_rating': 'Medio',
                        'is_resident': is_resident_audit
                    })
                    handle_srs_update("medio")
                    next_question_requested = True
                if srs_cols[2].button("Fácil", key=f"srs_easy_{question_id}"):
                    log_event(st.session_state.current_user, 'difficulty_rated', {
                        'question_id': question_id,
                        'user_rating': 'Fácil',
                        'is_resident': is_resident_audit
                    })
                    handle_srs_update("fácil")
                    next_question_requested = True

    return next_question_requested

def show_evaluation_page():
    """
    Página principal de evaluación en Flujo Infinito. Muestra siempre una pregunta
    y utiliza render_question_card para la interacción.
    """
    # --- 1. Lógica de Cabeceras de Modo (Solo para práctica por tema) ---
    if st.session_state.get('practice_mode'):
        mode_label = st.session_state.get('selected_tag') or st.session_state.get('practice_specialty')
        if mode_label:
            st.info(f"🚀 Entrenamiento Activo: '{mode_label}'")
        
        if st.button("⬅️ Volver a Biblioteca"):
            st.session_state.practice_mode = False
            if 'selected_tag' in st.session_state: del st.session_state.selected_tag
            if 'practice_specialty' in st.session_state: del st.session_state.practice_specialty
            if 'practice_topics' in st.session_state: del st.session_state.practice_topics
            st.session_state.current_page = "topics"
            if 'current_eval_question_data' in st.session_state:
                del st.session_state['current_eval_question_data']
            if 'last_displayed_id' in st.session_state:
                del st.session_state['last_displayed_id']
            st.rerun()
        st.markdown("---")

    # --- 2. Gestión de la Pregunta Actual (Flujo Infinito) ---
    if 'current_eval_question_data' not in st.session_state:
        st.session_state.current_eval_question_data = get_next_question_for_user(st.session_state.current_user)

    q_data = st.session_state.current_eval_question_data

    if q_data is None:
        # --- AUDITORIA: Manejo de Fin de Sesión (Cierre Limpio) ---
        if st.session_state.get('practice_mode'):
            st.balloons()
            st.success("🎉 ¡Entrenamiento Completado!")
            
            stats = st.session_state.get('session_stats', {'correct': 0, 'total': 0})
            score = int((stats['correct'] / stats['total'] * 100)) if stats['total'] > 0 else 0
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Preguntas", stats['total'])
            col2.metric("Aciertos", stats['correct'])
            col3.metric("Precisión", f"{score}%")
            
            st.markdown("---")
            if st.button("🔄 Volver a Biblioteca", type="primary", use_container_width=True):
                # Limpieza final
                del st.session_state.practice_mode
                st.session_state.current_page = "topics"
                st.rerun()
        else:
            st.warning("No hay preguntas en el sistema. ¡Crea algunas para empezar a estudiar!")
        return

    q_id = q_data['id']
    is_advance = q_data['is_advance']

    # --- 3. Notificación de Transición y Feedback Visual ---
    if is_advance and not st.session_state.get('previous_is_advance', False):
        st.toast('🎉 ¡Meta diaria cumplida! Entrando en Modo Infinito...', icon='🚀')

    if is_advance:
        st.caption("🔵 Modo Adelanto (Bonus FSRS)")
    else:
        st.caption("🔴 Repaso Prioritario / Nuevo")

    # --- 4. Renderizado de la Pregunta ---
    next_requested = render_question_card(q_id)
    
    if next_requested:
        st.session_state.previous_is_advance = is_advance
        del st.session_state.current_eval_question_data
        st.rerun()

def show_topics_page():
    """
    Biblioteca Simplificada: Entrenamiento Intensivo por Especialidad.
    Permite seleccionar una especialidad y entrenar con todos sus temas asociados.
    """
    st.header("📚 Biblioteca de Especialidades")
    st.caption("Entrenamiento enfocado: Selecciona una especialidad para iniciar un ciclo intensivo.")

    conn = get_db_conn()
    
    # 1. Filtro Maestro: Usar categorías reales del sistema (Escalable)
    specialties = get_all_categories()
    
    selected_spec = st.selectbox(
        "Selecciona una Especialidad:", 
        options=specialties, 
        index=None, 
        placeholder="Ej: Cardiología, Pediatría..."
    )

    if selected_spec:
        st.markdown("---")
        
        # 2. Consolidación: Contar preguntas reales (Dinámico - Match Exacto)
        count_query = "SELECT COUNT(*) FROM questions WHERE tag_categoria = ? AND status = 'active'"
        real_count = conn.execute(count_query, (selected_spec,)).fetchone()[0]
        
        # Estadísticas
        st.info(f"**{selected_spec}** incluye **{real_count}** preguntas activas en el banco.")

        # 3. Botón de Acción Único
        if st.button(f"🚀 Iniciar Entrenamiento Intensivo de {selected_spec}", type="primary", use_container_width=True):
            # 4. Inyección al Generador
            st.session_state.practice_mode = True
            st.session_state.practice_specialty = selected_spec
            # Eliminamos practice_topics para evitar restricciones
            if 'practice_topics' in st.session_state: del st.session_state.practice_topics
            # --- AUDITORIA: Inicializar rastreo de sesión ---
            st.session_state.session_answered_ids = []
            st.session_state.session_stats = {'correct': 0, 'total': 0}
            # ----------------------------------------------
            
            # Limpiar tag específico si existía
            if 'selected_tag' in st.session_state:
                del st.session_state.selected_tag
            
            # Resetear estado de evaluación
            st.session_state.current_page = "evaluacion"
            reset_evaluation_state()
            st.rerun()
    
    conn.close()

def get_fsrs_analytics(username):
    """Obtiene datos crudos de FSRS para análisis avanzado."""
    conn = get_db_conn()
    query = """
        SELECT 
            q.tag_categoria,
            q.tag_tema,
            p.stability,
            p.difficulty
        FROM progress p
        JOIN questions q ON p.question_id = q.id
        WHERE p.username = ?
    """
    try:
        return pd.read_sql_query(query, conn, params=(username,))
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()

def show_stats_page():
    """Muestra un dashboard analítico con un sistema de clasificación automática."""
    st.header("📊 Dashboard Analítico de la Comunidad")
    
    conn = get_db_conn()

    # Bloque de extracción de datos para el gráfico de Radar.
    # Se ejecuta una consulta para obtener el rendimiento por tema del usuario.
    sql_radar = """
        SELECT
            q.tag_categoria AS tag,
            COUNT(*) as total_preguntas,
            SUM(CASE WHEN p.interval > 3 THEN 1 ELSE 0 END) as preguntas_dominadas
        FROM questions q
        JOIN progress p ON q.id = p.question_id
        WHERE
            q.status = 'active'
            AND q.tag_categoria IS NOT NULL
            AND q.tag_categoria != ''
            AND p.username = ?
        GROUP BY tag
        ORDER BY total_preguntas DESC
        LIMIT 6
    """
    df_radar = pd.read_sql_query(sql_radar, conn, params=(st.session_state.current_user,))

    if not df_radar.empty:
        df_radar['Puntaje'] = (df_radar['preguntas_dominadas'] / df_radar['total_preguntas']) * 100

    if not df_radar.empty:
        st.subheader("🎯 Tu Radar Clínico")
        # Crear el gráfico
        fig = px.line_polar(
            df_radar,
            r='Puntaje',
            theta='tag',
            line_close=True,
            range_r=[0, 100],  # Escala fija de 0 a 100%
        )
        fig.update_traces(fill='toself') # Relleno de color sólido
        # Mostrar en Streamlit
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Responde preguntas de diferentes temas para activar tu Radar Clínico.")
    
    # 1. Extracción de Datos Granulares
    total_questions_global = conn.execute("SELECT COUNT(*) as count FROM questions WHERE status = 'active'").fetchone()['count']
    
    # Query para obtener todos los datos base de usuarios y su progreso
    query = """
        SELECT 
            u.username,
            u.is_resident,
            u.is_reference_model,
            u.total_active_days,
            u.current_streak,
            COALESCE(SUM(p.aciertos), 0) as total_aciertos,
            COALESCE(SUM(p.fallos), 0) as total_fallos,
            COALESCE(SUM(CASE WHEN p.interval > 7 THEN 1 ELSE 0 END), 0) as mastered_count
        FROM 
            users u
        LEFT JOIN 
            progress p ON u.username = p.username
        WHERE
            u.role != 'admin' AND u.status = 'active' AND u.username != 'guest_mode'
        GROUP BY
            u.username, u.is_resident, u.is_reference_model
    """
    df = pd.read_sql_query(query, conn)
    
    if df.empty:
        st.info("No hay datos de progreso de usuarios para mostrar en el ranking.")
        conn.close()
        return

    # 2. Transformación y Cálculo de Métricas
    df['total_answers'] = df['total_aciertos'] + df['total_fallos']
    df['accuracy'] = (df['total_aciertos'] / df['total_answers'] * 100).fillna(0.0)
    df['mastery'] = (df['mastered_count'] / total_questions_global * 100) if total_questions_global > 0 else 0.0

    # --- CALCULAR PROMEDIO DE RESIDENTES ---
    # Filtrar usuarios que son residentes (is_resident == 1)
    resident_data = df[df['is_resident'] == 1]
    
    # Calcular promedio o usar valor por defecto si no hay residentes
    if not resident_data.empty:
        avg_resident_accuracy = resident_data['accuracy'].mean()
    else:
        avg_resident_accuracy = 85.0  # Valor base por defecto
    
    # Mostrar métrica en consola para depuración
    print(f"📊 Promedio Precisión Residentes: {avg_resident_accuracy:.1f}%")

    # 3. Algoritmo de Etiquetado (Clasificación)
    def get_status_label(row, threshold):
        """Asigna una etiqueta de rango al usuario basada en su rendimiento."""
        # Jerarquía Absoluta: Si es el Fantasma/Modelo, es el Residente Supremo.
        if row.get('is_reference_model') == 1:
            return "🎓 Residente"
        
        if row['is_resident'] == 1:
            return "🎓 Residente"
            
        # --- AQUI SIGUE LA LÓGICA EXISTENTE DE PRECISIÓN ---
        if row['accuracy'] >= (threshold * 0.98) and row['total_answers'] > 50:
            return "⭐ Experto"
        if row['accuracy'] < 60.0 or (row['total_answers'] > 20 and row['mastery'] < 10.0):
            return "🚑 En Riesgo"
        return "🦁 Estudiante"

    # Se pasa 'avg_resident_accuracy' como argumento a la función apply.
    df['Estado'] = df.apply(get_status_label, axis=1, args=(avg_resident_accuracy,))

    # --- INICIO: LÓGICA DE ORDENAMIENTO DEL RANKING ---
    # 1. Ordenar: Constancia (Rey) -> Precisión -> Maestría
    df = df.sort_values(by=['total_active_days', 'accuracy', 'mastery'], ascending=[False, False, False])
    # 2. Resetear índice para que empiece en 0 el orden nuevo
    df = df.reset_index(drop=True)
    # 3. Crear columna de Posición (#) basada en el nuevo índice
    df.insert(0, '#', df.index + 1)
    # --- FIN: LÓGICA DE ORDENAMIENTO ---

    # Lógica de Racha para Display
    df['dias_acumulados_display'] = df.apply(
        lambda row: f"🔥 {row['total_active_days']}" if row['current_streak'] >= 3 else f"{row['total_active_days']}",
        axis=1
    )

    # --- INICIO: GRÁFICO COMPARATIVO DE RENDIMIENTO ---
    st.subheader("📈 Tu Rendimiento vs. La Comunidad")

    # 1. Cálculo de Métricas con Pandas
    # Nota: Se usa 'current_user' que es la variable correcta en st.session_state para esta app.
    user_accuracy_row = df[df['username'] == st.session_state.current_user]
    val_tu = user_accuracy_row['accuracy'].iloc[0] if not user_accuracy_row.empty else 0.0
    
    val_comunidad = df['accuracy'].mean()
    # El df ya está ordenado, por lo que .head(10) obtiene los mejores usuarios.
    val_top10 = df.head(10)['accuracy'].mean()

    # --- INICIO: BLOQUE DE DEBUG AUDITORÍA ---
    print("\n" + "="*40)
    print("🕵️‍♂️ AUDITORÍA DE DATOS DEL GRÁFICO")
    print("="*40)
    # 1. Verificar población total
    print(f"👥 Total de Usuarios en DataFrame: {len(df)}")

    # 2. Verificar datos de tu usuario
    # Corregido a 'current_user' y formato de if/else
    user_row_debug = df[df['username'] == st.session_state.current_user]
    if not user_row_debug.empty:
        tu_data = user_row_debug.iloc[0]
        print(f"👤 TÚ ({tu_data['username']}): Constancia={tu_data['total_active_days']} días | Precisión={tu_data['accuracy']:.2f}%")
    else:
        print("👤 TÚ: No encontrado en el ranking.")

    # 3. Verificar el Top 10 seleccionado
    top_10_debug = df.head(10)
    print("\n🏆 TOP 10 SELECCIONADOS (Orden actual):")
    print(top_10_debug[['username', 'total_active_days', 'accuracy', 'mastery']].to_string(index=False))

    # 4. Verificar los promedios matemáticos
    prom_comunidad = df['accuracy'].mean()
    prom_top10 = top_10_debug['accuracy'].mean()
    print(f"\n🧮 CÁLCULOS INTERNOS:")
    print(f"Promedio Comunidad: {prom_comunidad:.4f}%")
    print(f"Promedio Top 10: {prom_top10:.4f}%")
    print("="*40 + "\n")
    # --- FIN: BLOQUE DE DEBUG AUDITORÍA ---
    
    # 2. Preparación del DataFrame para el gráfico
    data_comp = pd.DataFrame({
        'Comparativa': ['Tú', 'Promedio Comunidad', 'Top 10 Expertos'],
        'Precisión': [val_tu, val_comunidad, val_top10],
        'Color': ['#3b82f6', '#9ca3af', '#eab308']  # Azul Vivo, Gris Neutro, Dorado Brillante
    })

    # 3. Visualización (Altair) - Barras + Texto
    bars = alt.Chart(data_comp).mark_bar().encode(
        x=alt.X('Comparativa:N', sort=None, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y('Precisión:Q', title='Precisión (%)', axis=alt.Axis(grid=False)),
        color=alt.Color('Color:N', scale=None, legend=None),
        tooltip=['Comparativa', alt.Tooltip('Precisión', title='Precisión', format='.1f')]
    )

    text = bars.mark_text(
        align='center',
        baseline='bottom',
        dy=-10  # Mueve el texto 10px por encima de la barra
    ).encode(
        text=alt.Text('Precisión:Q', format='.1f')
    )

    chart = (bars + text).configure_view(strokeWidth=0)

    st.altair_chart(chart, use_container_width=True)
    st.markdown("---") # Separador visual antes de la tabla de ranking
    # --- FIN: GRÁFICO COMPARATIVO DE RENDIMIENTO ---

    # 4. Preparación para Visualización
    df_display = df[['#', 'username', 'Estado', 'dias_acumulados_display', 'accuracy', 'mastery', 'total_answers']].copy()
    df_display.rename(columns={
        'username': 'Usuario',
        'accuracy': 'Precisión',
        'mastery': 'Maestría',
        'total_answers': 'Respuestas',
        'dias_acumulados_display': 'Días Acumulados'
    }, inplace=True)

    st.dataframe(
        df_display,
        column_config={
            "#": st.column_config.NumberColumn("Pos.", width="small", format="%d"),
            "Usuario": "Usuario",
            "Estado": "Estado",
            "Días Acumulados": "Días",
            "Precisión": st.column_config.ProgressColumn(
                "Precisión",
                help="Porcentaje de respuestas correctas (Aciertos / Totales).",
                format="%.1f%%", min_value=0, max_value=100,
            ),
            "Maestría": st.column_config.ProgressColumn(
                "Maestría",
                help="Porcentaje de preguntas del sistema dominadas (intervalo > 7 días).",
                format="%.1f%%", min_value=0, max_value=100,
            ),
        },
        use_container_width=True,
        hide_index=True,
        column_order=("#", "Usuario", "Estado", "Días Acumulados", "Precisión", "Maestría", "Respuestas")
    )

    # --- INICIO: Dashboard FSRS (Exclusivo Intensivo/MAFU) ---
    try:
        # Verificación de Privilegios
        user_priv_row = conn.execute("SELECT is_intensive, strategy_mode FROM users WHERE username = ?", (st.session_state.current_user,)).fetchone()
        user_is_intensive = bool(user_priv_row['is_intensive']) if user_priv_row else False
        strategy_mode = user_priv_row['strategy_mode'] if user_priv_row else 'STANDARD'
        
        if user_is_intensive or strategy_mode == 'MAFU':
            st.markdown("---")
            st.subheader("🧠 Metacognición FSRS (Exclusivo Intensivo)")
            
            df_fsrs = get_fsrs_analytics(st.session_state.current_user)
            
            if not df_fsrs.empty:
                # Gráfico 1: Radar de Memoria (Bar Chart)
                df_radar = df_fsrs.groupby('tag_categoria')['stability'].mean().reset_index()
                fig_radar = px.bar(
                    df_radar,
                    x='tag_categoria',
                    y='stability',
                    title="Radar de Memoria: Estabilidad por Categoría",
                    labels={'stability': 'Estabilidad Promedio (Días)', 'tag_categoria': 'Categoría'},
                    color='stability',
                    color_continuous_scale='Bluered'
                )
                st.plotly_chart(fig_radar, use_container_width=True)
                st.caption("Insight: Barras cortas = Peligro de Olvido. Barras largas = Memoria Sólida.")
                
                # Gráfico 2: Matriz de Dolor (Scatter Plot)
                df_topics = df_fsrs[df_fsrs['tag_tema'].notna() & (df_fsrs['tag_tema'] != '')]
                if not df_topics.empty:
                    df_matrix = df_topics.groupby('tag_tema').agg({
                        'difficulty': 'mean',
                        'stability': 'mean',
                        'tag_categoria': 'count'
                    }).rename(columns={'tag_categoria': 'count'}).reset_index()
                    
                    fig_matrix = px.scatter(
                        df_matrix,
                        x='difficulty',
                        y='stability',
                        size='count',
                        hover_name='tag_tema',
                        title="Matriz de Dolor: Dificultad vs Estabilidad",
                        labels={'difficulty': 'Dificultad Promedio (1-10)', 'stability': 'Estabilidad (Días)'},
                        color='difficulty',
                        color_continuous_scale='RdYlGn_r' # Verde (Bajo) -> Rojo (Alto)
                    )
                    st.plotly_chart(fig_matrix, use_container_width=True)
                    st.caption("Insight: Lo que esté abajo a la derecha (Alta dificultad, Baja estabilidad) es la 'Zona de Muerte' del estudiante.")
            else:
                st.info("Aún no tienes suficientes datos de estudio FSRS para generar metacognición.")
                
    except Exception as e:
        st.error(f"Error cargando Dashboard FSRS: {e}")
    # --- FIN: Dashboard FSRS ---

    conn.close()

def show_manage_questions_page():
    """Permite gestionar (Editar y Eliminar) preguntas con confirmación de borrado, agrupadas por categoría."""
    if 'editing_question_id' not in st.session_state:
        st.session_state.editing_question_id = None
    
    if 'confirm_delete_id' not in st.session_state:
        st.session_state.confirm_delete_id = None

    is_admin = (st.session_state.user_role == 'admin')
    
    # --- VISTA DE EDICIÓN (TOMA PRIORIDAD) ---
    if st.session_state.editing_question_id is not None:
        q_id = st.session_state.editing_question_id
        st.subheader(f"✏️ Editando Pregunta ID: {q_id}")
        conn = get_db_conn()
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (q_id,)).fetchone()
        conn.close()
        if not row:
            st.error("La pregunta no se encontró.")
            st.session_state.editing_question_id = None
            st.rerun()

        # Cargar categorías dinámicamente
        all_categories = get_all_categories()
        try:
            # Búsqueda segura del índice
            cat_index = all_categories.index(row['tag_categoria']) if row['tag_categoria'] in all_categories else None
        except (ValueError, TypeError):
            cat_index = None
        
        with st.form("edit_question_form"):
            new_enunciado = st.text_area("Enunciado", value=row['enunciado'])
            ops = row['opciones'].split('|')
            op_a, op_b, op_c, op_d = ops[0], ops[1], ops[2], ops[3]
            op_a = st.text_input("Opción A", value=op_a)
            op_b = st.text_input("Opción B", value=op_b)
            op_c = st.text_input("Opción C", value=op_c)
            op_d = st.text_input("Opción D", value=op_d)
            new_correcta_idx = st.radio("Respuesta Correcta", (0, 1, 2, 3), format_func=lambda x: f"Opción {chr(65+x)}")
            new_retro = st.text_area("Retroalimentación", value=row['retroalimentacion'])
            new_cat = st.selectbox("Categoría", options=all_categories, index=cat_index)
            new_tema = st.text_input("Tema", value=row['tag_tema'] or "")
            
            save_btn, cancel_btn = st.columns(2)
            if save_btn.form_submit_button("💾 Guardar Cambios", type="primary"):
                new_opciones = "|".join([op_a, op_b, op_c, op_d])
                correcta_val = [op_a, op_b, op_c, op_d][new_correcta_idx]
                conn = get_db_conn()
                conn.execute("UPDATE questions SET enunciado=?, opciones=?, correcta=?, retroalimentacion=?, tag_categoria=?, tag_tema=? WHERE id=?", (new_enunciado, new_opciones, correcta_val, new_retro, new_cat, new_tema, q_id))
                conn.commit()
                conn.close()
                st.success("Pregunta actualizada.")
                st.session_state.editing_question_id = None
                st.rerun()
            if cancel_btn.form_submit_button("❌ Cancelar"):
                st.session_state.editing_question_id = None
                st.rerun()
        return

    # --- VISTA PRINCIPAL (LISTADO POR CATEGORÍAS) ---
    st.subheader("🔑 Gestionar Preguntas" if is_admin else "📋 Mis Preguntas")
    conn = get_db_conn()
    
    # Query para Admins (trae todo) o Usuarios (solo las suyas)
    if is_admin:
        query = "SELECT id, enunciado, owner_username, status, tag_categoria FROM questions ORDER BY id DESC"
        params = ()
    else:
        query = "SELECT id, enunciado, owner_username, status, tag_categoria FROM questions WHERE owner_username = ? ORDER BY id DESC"
        params = (st.session_state.current_user,)
    
    preguntas = conn.execute(query, params).fetchall()
    conn.close()

    if not preguntas:
        st.info("No hay preguntas registradas.")
    else:
        # 1. Buscador
        search_q = st.text_input("🔍 Buscar en banco de preguntas:", "").lower().strip()

        # 2. Filtrado y Agrupación
        grouped_questions = {}
        for p in preguntas:
            # Filtro de texto
            if search_q and search_q not in p['enunciado'].lower():
                continue

            # Agrupación
            cat = p['tag_categoria'] if p['tag_categoria'] else "General / Sin Etiqueta"
            if cat not in grouped_questions:
                grouped_questions[cat] = []
            grouped_questions[cat].append(p)
            
        # 3. Renderizado por Categorías
        if not grouped_questions:
            st.warning(f"🚫 No se encontraron preguntas que coincidan con '{search_q}'.")
        else:
            for category in sorted(grouped_questions.keys()):
                count = len(grouped_questions[category])
                with st.expander(f"📂 {category} ({count})", expanded=False):
                    for preg in grouped_questions[category]:
                        # --- INICIO DEL CÓDIGO ORIGINAL DE LA TARJETA ---
                        pregunta_id = preg['id']
                        with st.container(border=True):
                            col_main, col_buttons = st.columns([0.8, 0.2])

                            with col_main:
                                col_main.write(preg['enunciado'])
                                
                                if preg['status'] == 'needs_revision':
                                    col_main.warning("⚠️ En Revisión")
                                
                                if is_admin:
                                    col_main.caption(f"Autor: {preg['owner_username']}")

                            if st.session_state.confirm_delete_id == pregunta_id:
                                with col_main:
                                    st.warning("¿Seguro que deseas eliminar esta pregunta?")
                                
                                with col_buttons:
                                    confirm_col1, confirm_col2 = st.columns(2)
                                    
                                    if confirm_col1.button("Sí, eliminar", key=f"confirm_del_{pregunta_id}", type="primary"):
                                        conn = get_db_conn()

                                        # --- SECURITY CHECK (IDOR) ---
                                        # Verificar en DB quién es el dueño real antes de borrar
                                        check_owner = conn.execute("SELECT owner_username FROM questions WHERE id = ?", (pregunta_id,)).fetchone()
                                        if not check_owner:
                                            st.error("La pregunta ya no existe.")
                                            st.stop()
                                            
                                        real_owner = check_owner[0]
                                        current_user = st.session_state.current_user
                                        user_role = st.session_state.user_role
                                        
                                        # Solo pasa si eres el dueño O eres admin
                                        if real_owner != current_user and user_role != 'admin':
                                            st.error("🚨 ALERTA DE SEGURIDAD: Intento de modificación no autorizado detectado.")
                                            # (Opcional) Podríamos loguear esto, pero por ahora detenemos la ejecución.
                                            st.stop()
                                        # --- FIN SECURITY CHECK ---

                                        conn.execute("DELETE FROM questions WHERE id = ?", (pregunta_id,))
                                        conn.commit()
                                        conn.close()
                                        st.success(f"Pregunta {pregunta_id} eliminada.")
                                        st.session_state.confirm_delete_id = None
                                        st.rerun()
                                    
                                    if confirm_col2.button("Cancelar", key=f"cancel_del_{pregunta_id}"):
                                        st.session_state.confirm_delete_id = None
                                        st.rerun()
                            else:
                                with col_buttons:
                                    if st.button("✏️ Editar", key=f"edit_{pregunta_id}"):
                                        st.session_state.editing_question_id = pregunta_id
                                        st.rerun()
                                    
                                    if st.button("🗑️ Eliminar", key=f"del_{pregunta_id}", type="primary"):
                                        st.session_state.confirm_delete_id = pregunta_id
                                        st.rerun()
                        # --- FIN DEL CÓDIGO ORIGINAL DE LA TARJETA ---

# --- INICIO DE SECCIÓN NUEVA: PÁGINA DE DUELOS ---
def play_duel_interface():
    """
    Maneja la interfaz de un duelo, el historial de respuestas y el resumen final.
    """
    duel_id = st.session_state.current_duel_id
    q_idx = st.session_state.duel_question_index
    questions = st.session_state.duel_questions
    
    # --- 1. LÓGICA DE FIN DE DUELO ---
    if q_idx >= len(questions):
        st.success("¡Has completado el duelo!")
        st.balloons()
        
        # --- 2. INICIO: Resumen Detallado de Desempeño ---
        st.subheader("Resumen Detallado de Desempeño")

        if 'duel_history' in st.session_state and st.session_state.duel_history:
            for i, record in enumerate(st.session_state.duel_history):
                enunciado_corto = (record['enunciado'][:60] + '...') if len(record['enunciado']) > 60 else record['enunciado']
                
                # Definir encabezado del expander según el resultado
                if record['is_timeout']:
                    header = f"Pregunta {i+1}: ⏰ Tiempo Agotado - {enunciado_corto}"
                elif record['correct']:
                    header = f"Pregunta {i+1}: ✅ Correcto - {enunciado_corto}"
                else:
                    header = f"Pregunta {i+1}: ❌ Incorrecto - {enunciado_corto}"

                with st.expander(header):
                    st.markdown(f"**Enunciado:** {record['enunciado']}")
                    
                    if record['is_timeout']:
                        st.warning("No se registró respuesta por tiempo.")
                    else:
                        st.write(f"**Tu respuesta:** {record['opcion_elegida']}")

                    st.write(f"**Respuesta Correcta:** {record['opcion_correcta']}")
                    st.markdown("---")
                    st.info(f"**Retroalimentación:**\n\n{record['retroalimentacion']}")
        else:
            st.info("No hay historial de duelo para mostrar.")
        # --- FIN: Resumen Detallado de Desempeño ---

        conn = get_db_conn()
        cursor = conn.cursor()
        current_user = st.session_state.current_user
        score = st.session_state.duel_user_score
        
        duel = cursor.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone()
        
        # Actualizar puntaje del usuario actual
        if duel['challenger_username'] == current_user:
            cursor.execute("UPDATE duels SET challenger_score = ? WHERE id = ?", (score, duel_id))
            opponent_finished = duel['opponent_score'] is not None
            opponent_score = duel['opponent_score']
        else: # es oponente
            cursor.execute("UPDATE duels SET opponent_score = ? WHERE id = ?", (score, duel_id))
            opponent_finished = True
            opponent_score = duel['challenger_score']
        conn.commit()

        # --- 3. Anuncio del Ganador (Debajo del resumen) ---
        if opponent_finished:
            user_score = score if score is not None else 0
            opponent_score_val = opponent_score if opponent_score is not None else 0
            is_tie = (user_score == opponent_score_val)

            if user_score > opponent_score_val:
                winner = current_user
            elif opponent_score_val > user_score:
                winner = duel['challenger_username'] if duel['challenger_username'] != current_user else duel['opponent_username']
            else:  # Empate
                winner = duel['challenger_username']  # Empate gana el retador
            
            cursor.execute("UPDATE duels SET status = 'finished', winner = ? WHERE id = ?", (winner, duel_id))
            conn.commit()
            
            st.markdown("---")
            st.subheader("Resultado Final del Duelo")

            if is_tie:
                st.warning(f"🤝 Hubo un empate ({user_score} a {opponent_score_val}). El retador ('{winner}') gana por regla.")
            elif winner == current_user:
                st.success(f"🏆 ¡Ganaste el duelo! Resultado: {user_score} a {opponent_score_val}.")
            else:
                st.error(f"💔 Perdiste el duelo contra '{winner}'. Resultado: {user_score} a {opponent_score_val}.")

        conn.close()
        
        if st.button("Volver a Duelos"):
            del st.session_state.duel_state
            if 'duel_history' in st.session_state:
                del st.session_state.duel_history # Limpiar historial
            st.rerun()
        return

    # --- 4. LÓGICA DE PREGUNTA EN CURSO ---
    if 'duel_question_start_time' not in st.session_state:
        st.session_state.duel_question_start_time = datetime.datetime.now()

    pregunta = questions[q_idx]

    st.warning("⚠️ Tienes 40 segundos. Si respondes tarde, la pregunta contará como fallida.")
    st.subheader(f"Pregunta {q_idx + 1}/{len(questions)}")
    st.markdown(f"### {pregunta['enunciado']}")

    with st.form(f"duel_q_{pregunta['id']}", clear_on_submit=True):
        opciones = pregunta['opciones'].split('|')
        user_choice = st.radio("Elige una respuesta:", options=opciones, key=f"duel_radio_{pregunta['id']}")
        
        if st.form_submit_button("Responder"):
            tiempo_usado = (datetime.datetime.now() - st.session_state.duel_question_start_time).total_seconds()
            
            is_timeout = tiempo_usado > 40
            is_correct = user_choice == pregunta['correcta'] and not is_timeout

            # --- 5. Captura de Datos para el historial ---
            history_record = {
                'enunciado': pregunta['enunciado'],
                'opcion_elegida': user_choice if not is_timeout else "Ninguna (Tiempo Agotado)",
                'opcion_correcta': pregunta['correcta'],
                'retroalimentacion': pregunta['retroalimentacion'],
                'is_timeout': is_timeout,
                'correct': is_correct
            }
            st.session_state.duel_history.append(history_record)

            if is_timeout:
                st.error("¡Tiempo agotado! Te demoraste más de 40 segundos.")
            else:
                if is_correct:
                    st.session_state.duel_user_score += 1
                    st.toast("¡Correcto! ✅")
                else:
                    st.toast("Incorrecto. ❌")
            
            st.session_state.duel_question_index += 1
            del st.session_state.duel_question_start_time
            st.rerun()

def show_duels_page():
    """Página principal de Duelos (PvP Asincrónico), excluyendo al admin de la lógica de juego."""
    st.header("⚔️ Duelos PvP")

    # 1. Identificar al Admin para excluirlo
    try:
        admin_user = st.secrets["ADMIN_USER"]
    except KeyError:
        admin_user = "admin"

    if 'duel_state' not in st.session_state:
        st.session_state.duel_state = 'overview'

    if st.session_state.duel_state == 'playing':
        play_duel_interface()
        return

    # --- VISTA GENERAL DE DUELOS ---
    conn = get_db_conn()
    cursor = conn.cursor()
    current_user = st.session_state.current_user

    # Sección A: Desafiar
    st.subheader("Desafiar a un Oponente")
    if st.button("🤺 Buscar Oponente Aleatorio", use_container_width=True, type="primary"):
        # 2. Modificar consulta para que no seleccione al admin como oponente
        cursor.execute(
            "SELECT username FROM users WHERE username != ? AND username != ? AND is_approved = 1 ORDER BY RANDOM() LIMIT 1",
            (current_user, admin_user)
        )
        opponent = cursor.fetchone()
        
        if not opponent:
            st.warning("No hay otros usuarios disponibles para desafiar.")
        else:
            opponent_username = opponent['username']
            cursor.execute("SELECT id FROM questions ORDER BY RANDOM() LIMIT 5")
            questions = cursor.fetchall()
            if len(questions) < 5:
                st.error("No hay suficientes preguntas en la base de datos para un duelo (se necesitan 5).")
            else:
                question_ids = ",".join([str(q['id']) for q in questions])
                now = datetime.datetime.now()
                
                cursor.execute(
                    "INSERT INTO duels (challenger_username, opponent_username, question_ids, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                    (current_user, opponent_username, question_ids, now)
                )
                duel_id = cursor.lastrowid
                conn.commit()
                
                # Inicialización del estado del duelo
                st.session_state.duel_state = 'playing'
                st.session_state.current_duel_id = duel_id
                st.session_state.duel_question_index = 0
                st.session_state.duel_user_score = 0
                st.session_state.duel_history = [] # INICIALIZAR HISTORIAL
                st.session_state.duel_questions = [dict(q) for q in conn.execute(f"SELECT * FROM questions WHERE id IN ({question_ids})").fetchall()]
                st.rerun()

    st.markdown("---")

    # Sección B: Duelos Pendientes
    st.subheader("Duelos Pendientes")
    pending_duels = cursor.execute(
        "SELECT * FROM duels WHERE opponent_username = ? AND status = 'pending' ORDER BY created_at DESC",
        (current_user,)
    ).fetchall()

    if not pending_duels:
        st.info("Nadie te ha desafiado... todavía.")
    else:
        for duel in pending_duels:
            with st.container(border=True):
                st.write(f"Has sido desafiado por **{duel['challenger_username']}**.")
                if st.button("🔥 Aceptar Duelo", key=f"accept_{duel['id']}"):
                    question_ids = duel['question_ids']
                    # Inicialización del estado del duelo
                    st.session_state.duel_state = 'playing'
                    st.session_state.current_duel_id = duel['id']
                    st.session_state.duel_question_index = 0
                    st.session_state.duel_user_score = 0
                    st.session_state.duel_history = [] # INICIALIZAR HISTORIAL
                    st.session_state.duel_questions = [dict(q) for q in conn.execute(f"SELECT * FROM questions WHERE id IN ({question_ids})").fetchall()]
                    st.rerun()
    
    st.markdown("---")

    # Sección de Estadísticas y Ranking
    st.subheader("Estadísticas y Ranking de Duelos")
    
    wins = cursor.execute("SELECT COUNT(*) FROM duels WHERE winner = ?", (current_user,)).fetchone()[0]
    losses = cursor.execute("SELECT COUNT(*) FROM duels WHERE winner != ? AND (challenger_username = ? OR opponent_username = ?)", (current_user, current_user, current_user)).fetchone()[0]
    
    col1, col2 = st.columns(2)
    col1.metric("Duelos Ganados", wins)
    col2.metric("Duelos Perdidos", losses)

    # 3. Modificar consulta del ranking para excluir al admin de los resultados
    st.markdown("##### Top Duelistas")
    ranking_df = pd.read_sql_query(
        "SELECT winner as Usuario, COUNT(id) as Victorias FROM duels WHERE winner IS NOT NULL AND winner != ? GROUP BY winner ORDER BY Victorias DESC",
        conn,
        params=(admin_user,)
    )
    if not ranking_df.empty:
        ranking_df.index += 1
        st.dataframe(ranking_df, use_container_width=True)
    else:
        st.info("Aún no hay resultados de duelos para mostrar un ranking.")

    conn.close()
# --- FIN DE SECCIÓN NUEVA ---

def get_user_analytics(username):
    conn = get_db_conn()
    # Traemos los últimos 500 eventos de respuesta
    query = """
        SELECT timestamp, metadata 
        FROM activity_log 
        WHERE username = ? AND action_type = 'answer_submitted' 
        ORDER BY id ASC
    """
    df = pd.read_sql_query(query, conn, params=(username,))
    
    if df.empty:
        return pd.DataFrame()

    # Procesamiento del JSON en metadatos
    parsed_data = []
    for index, row in df.iterrows():
        try:
            meta = json.loads(row['metadata'])
            parsed_data.append({
                'Fecha': pd.to_datetime(row['timestamp']),
                'Velocidad (s)': float(meta.get('time_seconds', 0)),
                'Resultado': meta.get('result', 'unknown'),
                # Lógica Anti-N/A: Busca 'difficulty' (nuevo) o 'ai_difficulty' (legacy) o fallback
                'Dificultad': meta.get('difficulty') or meta.get('ai_difficulty') or 'Media',
                'Tema': meta.get('topic', 'General')
            })
        except:
            continue # Saltar filas corruptas
            
    return pd.DataFrame(parsed_data)

def render_matrix_admin():
    """
    Renderiza el panel de administración para 'La Matriz', permitiendo
    la inyección de temas y la visualización de la cola de procesamiento.
    """
    st.header("🧬 Panel de Control de La Matriz")

    # --- INICIO: Dashboard de Métricas de Producción ---
    conn_metrics = get_db_conn()
    try:
        # KPIs
        total_questions = conn_metrics.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        # SQLite 'now' devuelve UTC. Usamos date() para comparar solo la fecha.
        questions_today = conn_metrics.execute("SELECT COUNT(*) FROM questions WHERE date(created_at) = date('now')").fetchone()[0]
        pending_topics = conn_metrics.execute("SELECT COUNT(*) FROM matrix_topics WHERE status='PENDIENTE'").fetchone()[0]
        cooldown_topics = conn_metrics.execute("SELECT COUNT(*) FROM matrix_topics WHERE status='COOLDOWN'").fetchone()[0]
        
        # DataFrames para gráficos
        df_dist = pd.read_sql_query("SELECT tag_categoria, COUNT(*) as count FROM questions WHERE tag_categoria IS NOT NULL GROUP BY tag_categoria", conn_metrics)
        df_top_topics = pd.read_sql_query("SELECT tag_tema, COUNT(*) as count FROM questions WHERE tag_tema IS NOT NULL GROUP BY tag_tema ORDER BY count DESC LIMIT 5", conn_metrics)
        
    except Exception as e:
        st.error(f"Error cargando dashboard: {e}")
        total_questions, questions_today, pending_topics, cooldown_topics = 0, 0, 0, 0
        df_dist, df_top_topics = pd.DataFrame(), pd.DataFrame()
    finally:
        conn_metrics.close()

    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("📦 Inventario Total", total_questions)
    kpi2.metric("⚡ Producción Hoy", questions_today)
    kpi3.metric("⏳ En Cola", pending_topics)
    kpi4.metric("🔥 En Cooldown", cooldown_topics)

    with st.expander("📊 Ver Distribución del Banco de Preguntas", expanded=True):
        c_chart1, c_chart2 = st.columns(2)
        
        with c_chart1:
            if not df_dist.empty:
                fig_pie = px.pie(df_dist, names='tag_categoria', values='count', title='Distribución por Especialidad')
                st.plotly_chart(fig_pie, use_container_width=True)
        
        with c_chart2:
            if not df_top_topics.empty:
                fig_bar = px.bar(df_top_topics, x='tag_tema', y='count', title='Top 5 Temas Recurrentes')
                st.plotly_chart(fig_bar, use_container_width=True)
    st.markdown("---")
    # --- FIN: Dashboard de Métricas de Producción ---

    # --- INICIO: Panel de Control del Núcleo ---
    st.subheader("🎛️ Panel de Control del Núcleo")
    
    conn_status = get_db_conn()
    try:
        status_row = conn_status.execute("SELECT value FROM system_config WHERE key = 'matrix_status'").fetchone()
        current_status = status_row['value'] if status_row else 'PAUSED'
        
        # Obtener Temas Pendientes
        pending_topics_rows = conn_status.execute("SELECT id, topic_name FROM matrix_topics WHERE UPPER(status)='PENDIENTE' ORDER BY topic_name").fetchall()
    except Exception:
        current_status = 'PAUSED'
        pending_topics_rows = []
    
    status_map = {
        'RUNNING': "🟢 CORRIENDO",
        'ONCE': "🟡 PROCESANDO UNO",
        'ONCE_SPECIFIC': "🎯 PROCESANDO ESPECÍFICO",
        'PAUSED': "🔴 PAUSADO"
    }
    
    st.markdown(f"**Estado Actual:** {status_map.get(current_status, current_status)}")
    
    col_play, col_pause = st.columns(2)

    with col_play:
        if st.button("▶️ ACTIVAR MATRIZ", type="primary", use_container_width=True):
            dbm.run_atomic_query("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'ACTIVE')")
            st.success("Matriz ACTIVADA. El worker comenzará a procesar.")
            time.sleep(1) # Dar tiempo a que DB actualice
            st.rerun()

    with col_pause:
        if st.button("⏸️ PAUSAR (Kill-Switch)", type="secondary", use_container_width=True):
            dbm.run_atomic_query("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
            st.warning("Matriz PAUSADA. El consumo se detendrá en el próximo ciclo.")
            time.sleep(1)
            st.rerun()

    # Botón de Emergencia para limpiar cola atascada
    col_reset, col_test = st.columns(2)
    
    with col_reset:
        if st.button("🧹 Limpiar Temas Atascados (Reset)", type="secondary", use_container_width=True):
            # Invocamos la lógica de recuperación (Safe atomic operation)
            temp_worker = matrix.MatrixWorker()
            temp_worker.emergency_recovery()
            st.success("Cola de procesamiento reiniciada. Los temas 'PROCESANDO' han vuelto a 'PENDIENTE'.")
            time.sleep(1)
            st.rerun()

    with col_test:
        if st.button("🐞 TEST SÍNCRONO (1 TEMA)", type="secondary", use_container_width=True):
            if current_status == 'ACTIVE':
                st.warning("⚠️ **ALERTA:** La Matriz está ACTIVA. Es probable que el Worker y tu Test compitan por los temas.")
                st.info("Pausa la Matriz primero para un diagnóstico limpio.")
            
            st.info("Iniciando prueba síncrona en Main Thread...")
            
            # Instanciar worker temporal
            debug_worker = matrix.MatrixWorker()
            
            # --- DIAGNÓSTICO DE LLAVE ---
            # Mostramos la llave en uso (máscara) para descartar que esté usando la vieja (vscode)
            api_key_debug, _ = debug_worker.get_config_values()
            masked_key = f"...{api_key_debug[-4:]}" if api_key_debug and len(api_key_debug) > 4 else "NO_SET"
            st.code(f"🔑 API Key en uso: {masked_key}", language="text")
            
            # Buscar tema
            topic = debug_worker.get_next_topic()
            
            if not topic:
                # Diagnóstico: ¿Por qué null?
                conn_diag = dbm.get_db_conn()
                cnt = conn_diag.execute("SELECT count(*) as c FROM matrix_topics WHERE status = 'PROCESANDO'").fetchone()['c']
                conn_diag.close()
                
                if cnt > 0:
                    st.error(f"❌ COLA BLOQUEADA: Hay {cnt} tema(s) en estado 'PROCESANDO'.")
                    st.write("👉 Solución: Pulsa 'Limpiar Temas Atascados' y asegúrate de que la Matriz esté PAUSADA.")
                else:
                    st.warning("📭 No hay temas pendientes en la fecha/prioridad seleccionada.")
            else:
                st.write(f"Procesando: {topic['topic_name']} (ID: {topic['id']})")
                
                # Ejecutar proceso BLOQUEANTE
                # Esto nos dirá si explota por API Key, Red, o SQL
                try:
                    success = debug_worker.execute_sequential_process(topic)
                    if success:
                        st.success(f"✅ ÉXITO: Tema {topic['id']} generado y guardado.")
                    else:
                        # Recuperar el mensaje de error real de la base de datos
                        conn_err = dbm.get_db_conn()
                        err_row = conn_err.execute("SELECT last_error FROM matrix_topics WHERE id = ?", (topic['id'],)).fetchone()
                        conn_err.close()
                        
                        real_error = err_row['last_error'] if err_row else "Error desconocido (Check logs)"
                        st.error(f"❌ FALLO TÉCNICO: {real_error}")
                        st.info("💡 Si dice 'Network Error' o 'HTTP', revisa tu API Key y cuotas.")
                except Exception as e:
                    st.error(f"❌ EXCEPCIÓN: {e}")
                    st.code(traceback.format_exc(), language="python")
                    
            st.warning("Prueba finalizada.")

    # Cerrar la conexión de lectura explícitamente al final del bloque de estado
    conn_status.close()
    
    st.markdown("---")
    
    # Selector de Objetivo
    topic_map = {row['topic_name']: row['id'] for row in pending_topics_rows}
    selected_topic_name = st.selectbox("🎯 Objetivo Único", options=list(topic_map.keys()))
    selected_target_id = topic_map.get(selected_topic_name)

    c1, c2, c3 = st.columns(3)
    if c1.button("▶️ INICIAR PROCESO", use_container_width=True):
        dbm.run_atomic_query("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'ACTIVE')")
        st.success('🚀 Modo Industrial Activado (Lógica ADN)')
        time.sleep(1); st.rerun()
        
    if c2.button("⏯️ PROCESAR SOLO SELECCIONADO", use_container_width=True):
        if selected_target_id:
            # Atomic para mult-statement es posible, pero aquí lo haremos en dos pasos o uno compuesto
            # Mejor usar atomic_query para cada uno o una función helper, pero para seguridad run_atomic abre y cierra.
            dbm.run_atomic_query("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'ONCE_SPECIFIC')")
            dbm.run_atomic_query("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_target_id', ?)", (str(selected_target_id),))
            st.success(f'🎯 Orden enviada: Procesar ID {selected_target_id} y pausar')
            time.sleep(1); st.rerun()
        else:
            st.warning("⚠️ Selecciona un tema primero.")
            
    if c3.button("⏸️ PAUSA TOTAL", use_container_width=True):
        dbm.run_atomic_query("INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_status', 'PAUSED')")
        # Forzamos kill switch en workers si fuera necesario (pero leen la config)
        st.success('🛑 Deteniendo maquinaria...')
        time.sleep(1); st.rerun()
    
    # conn_status ya fue cerrado arriba, no intentarlo cerrar de nuevo
    st.markdown("---")
    # --- FIN: Panel de Control del Núcleo ---

    # --- INICIO: Zona de Pre-producción (Triaje) ---
    st.markdown('### 🛡️ Zona de Pre-producción (Triaje)')
    
    conn_triaje = get_db_conn()
    try:
        pending_suggestions = conn_triaje.execute("SELECT * FROM suggested_topics WHERE status='PENDING' ORDER BY created_at DESC").fetchall()
        
        if not pending_suggestions:
            st.success("✅ Bandeja limpia. No hay sugerencias pendientes.")
        else:
            st.info(f"Hay {len(pending_suggestions)} sugerencias esperando revisión.")
            all_cats = get_all_categories()
            
            for row in pending_suggestions:
                sugg_id = row['id']
                raw_topic = row['raw_topic']
                suggester = row['suggester_name']
                
                with st.container(border=True):
                    c_info, c_edit, c_actions = st.columns([1, 2, 1])
                    
                    with c_info:
                        st.caption(f"Sugerido por: {suggester}")
                        st.write(f"Original: **{raw_topic}**")
                        
                    with c_edit:
                        corrected_topic = st.text_input("Tema Corregido", value=raw_topic, key=f"edit_topic_{sugg_id}")
                        target_cat = st.selectbox("Categoría", options=all_cats, key=f"cat_topic_{sugg_id}")
                        
                    with c_actions:
                        st.write("Acciones:")
                        col_approve, col_reject = st.columns(2)
                        
                        if col_approve.button("✅", key=f"approve_{sugg_id}", help="Aprobar y pasar a Producción"):
                            try:
                                # 1. Insertar en cola de producción
                                conn_triaje.execute(
                                    "INSERT INTO matrix_topics (topic_name, target_category, status, priority, created_at) VALUES (?, ?, 'PENDIENTE', 3, ?)",
                                    (corrected_topic, target_cat, datetime.datetime.now())
                                )
                                # 2. Marcar como aprobado
                                conn_triaje.execute("UPDATE suggested_topics SET status='APPROVED' WHERE id=?", (sugg_id,))
                                conn_triaje.commit()
                                st.success("Tema pasado a Producción")
                                time.sleep(0.5)
                                st.rerun()
                            except sqlite3.IntegrityError:
                                st.error("El tema ya existe en la cola.")
                            except Exception as e:
                                st.error(f"Error: {e}")

                        if col_reject.button("❌", key=f"reject_{sugg_id}", help="Rechazar sugerencia"):
                            conn_triaje.execute("UPDATE suggested_topics SET status='REJECTED' WHERE id=?", (sugg_id,))
                            conn_triaje.commit()
                            st.warning("Tema descartado")
                            time.sleep(0.5)
                            st.rerun()
    except Exception as e:
        st.error(f"Error cargando triaje: {e}")
    finally:
        conn_triaje.close()
    
    st.markdown("---")
    # --- FIN: Zona de Pre-producción ---

    # --- INICIO: Sincronización Masiva ---
    st.markdown("---")
    st.subheader("🔄 Sincronización Masiva")
    st.info("Esta acción lee todos los temas del Cronograma Maestro (tabla `topics`) y los añade a la cola de procesamiento si aún no existen. Es ideal para una carga inicial del sistema.")
    if st.button("Sincronizar Cronograma con la Cola de Procesamiento", type="primary", use_container_width=True):
        conn_sync = get_db_conn()
        try:
            # 1. Leer todos los temas del cronograma maestro (topics)
            master_topics = conn_sync.execute("SELECT nombre_tema, especialidad FROM topics").fetchall()
            
            # 2. Preparar los datos para la inserción en la cola (matrix_topics)
            now = datetime.datetime.now()
            topics_to_insert = [
                (row['nombre_tema'], 3, 'PENDIENTE', now, row['especialidad'])
                for row in master_topics
            ]
            
            # 3. Insertar usando INSERT OR IGNORE para evitar duplicados
            cursor = conn_sync.cursor()
            cursor.executemany(
                "INSERT OR IGNORE INTO matrix_topics (topic_name, priority, status, created_at, target_category) VALUES (?, ?, ?, ?, ?)",
                topics_to_insert
            )
            
            inserted_count = cursor.rowcount
            conn_sync.commit()
            
            st.success(f"✅ Sincronización completada. Se añadieron {inserted_count} nuevos temas a la cola.")
            st.balloons()
        finally:
            if conn_sync:
                conn_sync.close()
    # --- FIN: Sincronización Masiva ---

    # --- 0. Bloque de Configuración de Credenciales ---
    with st.expander("🔑 Configuración de Credenciales", expanded=False):
        conn_cfg = get_db_conn()
        
        # Recuperar el valor actual de la API Key
        current_api_key_row = conn_cfg.execute("SELECT value FROM system_config WHERE key = 'gemini_api_key'").fetchone()
        current_api_key = current_api_key_row['value'] if current_api_key_row else ""
        
        new_api_key = st.text_input(
            "Gemini API Key",
            value=current_api_key,
            type="password",
            help="Pega tu clave de API de Google AI Studio aquí."
        )
        
        if st.button("Guardar Clave"):
            if new_api_key:
                conn_cfg.execute(
                    "INSERT OR REPLACE INTO system_config (key, value) VALUES ('gemini_api_key', ?)",
                    (new_api_key,)
                )
                print(f"💾 [FRONTEND] Intentando guardar en ruta absoluta: {os.path.abspath(dbm.DB_PATH)}")
                print(f"💾 [FRONTEND] Clave recibida: {new_api_key[:5]}********")
                conn_cfg.commit()
                st.success("¡Clave de API guardada con éxito!")
            else:
                st.warning("El campo de la clave de API no puede estar vacío.")
        
        conn_cfg.close()

    # --- Bloque de Configuración del Prompt Maestro ---
    with st.expander("🧠 Configuración del Prompt Maestro", expanded=False):
        conn_prompt = get_db_conn()
        try:
            prompt_row = conn_prompt.execute("SELECT value FROM system_config WHERE key = 'matrix_prompt_template'").fetchone()
            
            default_prompt = """Actúa como un experto en {topic_name} creando preguntas de opción múltiple para un examen de residencia médica.
Genera 5 preguntas sobre {topic_name}.
Formato de salida: Un array JSON de objetos. Cada objeto debe tener:
- "enunciado": El texto de la pregunta (string).
- "opciones": Una lista de 4 posibles respuestas (list of strings). IMPORTANTE: Cada opción en la lista DEBE tener un prefijo de anclaje estático, por ejemplo: "[A] Opción 1", "[B] Opción 2".
- "correcta": La respuesta correcta exacta de la lista de opciones, INCLUYENDO el prefijo de anclaje (ej: "[C] Opción 3").
- "retroalimentacion": Una explicación detallada de por qué la respuesta es correcta (string).
- "tag_tema": Una etiqueta específica sobre el sub-tema tratado (ej: 'Manejo de Shock').
Asegúrate de que el JSON sea válido y completo."""

            current_prompt = prompt_row['value'] if prompt_row and prompt_row['value'] else default_prompt
            
            new_prompt = st.text_area(
                "Template del Prompt para Generación",
                value=current_prompt,
                height=300
            )

            if st.button("💾 Guardar Prompt"):
                conn_prompt.execute(
                    "INSERT OR REPLACE INTO system_config (key, value) VALUES ('matrix_prompt_template', ?)",
                    (new_prompt,)
                )
                conn_prompt.commit()
                st.success("¡Prompt guardado con éxito!")
                
        except Exception as e:
            st.error(f"Error al cargar/guardar el prompt: {e}")
        finally:
            if conn_prompt:
                conn_prompt.close()

    # --- SECCIÓN NUEVA: CONFIGURACIÓN DEL AUDITOR ---
    with st.expander("🛡️ Configuración del Prompt Auditor (Juez AI)"):
        st.info("Este prompt instruye a la IA secundaria que revisa la calidad de cada pregunta antes de guardarla.")
        
        # 1. Leer valor actual de la BD
        conn_audit = get_db_conn()
        try:
            row_audit = conn_audit.execute("SELECT value FROM system_config WHERE key = 'matrix_auditor_prompt'").fetchone()
            current_audit_prompt = row_audit['value'] if row_audit else "Prompt no definido..."
        except Exception as e:
            st.error(f"Error cargando prompt auditor: {e}")
            current_audit_prompt = ""
        finally:
            conn_audit.close()

        # 2. Área de Texto para Editar
        new_audit_prompt = st.text_area(
            "Instrucciones para el Juez:", 
            value=current_audit_prompt, 
            height=200,
            key="audit_prompt_area"
        )

        # 3. Botón de Guardado
        if st.button("💾 Guardar Configuración del Juez"):
            try:
                conn_save = get_db_conn()
                conn_save.execute(
                    "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", 
                    ('matrix_auditor_prompt', new_audit_prompt)
                )
                conn_save.commit()
                conn_save.close()
                st.success("✅ ¡El Juez ha sido re-entrenado con las nuevas órdenes!")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(f"Error guardando: {e}")

    # --- 3. SECCIÓN NUEVA: CONFIGURACIÓN VISUAL (TOGGLE) ---
    st.markdown("---") # Separador visual
    with st.expander("🎨 Configuración Visual (Interfaz)", expanded=True):
        st.info("Controla si las opciones muestran 'A)', 'B)' o solo el texto limpio.")
        
        # A. Leer estado actual de la DB
        conn_vis = get_db_conn()
        try:
            row_vis = conn_vis.execute("SELECT value FROM system_config WHERE key = 'show_option_prefixes'").fetchone()
            # Si no existe, por defecto es "True"
            show_prefixes_db = row_vis['value'] if row_vis else "True"
            is_checked = (show_prefixes_db == "True")
        except Exception:
            is_checked = True
        finally:
            conn_vis.close()
        
        # B. El Interruptor Visual
        enable_prefixes = st.toggle("Mostrar letras (A, B, C...) en las respuestas", value=is_checked)
        
        # C. Guardar cambio en DB si el usuario lo toca
        if enable_prefixes != is_checked:
            new_val = "True" if enable_prefixes else "False"
            conn_save = get_db_conn()
            conn_save.execute(
                "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", 
                ('show_option_prefixes', new_val)
            )
            conn_save.commit()
            conn_save.close()
            st.toast(f"✅ Visualización actualizada: {'CON Letras' if enable_prefixes else 'SIN Letras'}")
            time.sleep(0.5)
            st.rerun()

    # --- Bloque de Gestión de Categorías ---
    with st.expander("🏷️ Gestión de Especialidades", expanded=False):
        st.subheader("Especialidades Actuales")
        
        # Obtener y mostrar categorías
        all_categories = get_all_categories()
        if all_categories:
            st.pills("Categorías", options=all_categories, selection_mode="multi", key="pills_fix_final")
        else:
            st.info("No hay especialidades definidas en la base de datos.")

        st.markdown("---")

        # --- Formulario para Añadir ---
        st.subheader("➕ Añadir Nueva Especialidad")
        with st.form("add_category_form", clear_on_submit=True):
            new_category_name = st.text_input("Nombre de la nueva especialidad").strip()
            if st.form_submit_button("Añadir Especialidad"):
                if new_category_name:
                    conn_add = get_db_conn()
                    try:
                        conn_add.execute("INSERT OR IGNORE INTO medical_categories (name) VALUES (?)", (new_category_name,))
                        conn_add.commit()
                        st.success(f"Especialidad '{new_category_name}' añadida.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error al añadir: {e}")
                    finally:
                        conn_add.close()
                else:
                    st.warning("El nombre no puede estar vacío.")

        st.markdown("---")

        # --- Formulario para Eliminar ---
        st.subheader("🗑️ Eliminar Especialidad")
        if all_categories:
            with st.form("delete_category_form"):
                category_to_delete = st.selectbox("Selecciona la especialidad a eliminar", options=all_categories)
                st.warning("Advertencia: Eliminar una especialidad no borrará las preguntas que ya la usan, solo la quitará de las opciones futuras.", icon="⚠️")
                
                if st.form_submit_button("Eliminar Especialidad Seleccionada", type="primary"):
                    conn_del = get_db_conn()
                    try:
                        conn_del.execute("DELETE FROM medical_categories WHERE name = ?", (category_to_delete,))
                        conn_del.commit()
                        st.success(f"Especialidad '{category_to_delete}' eliminada.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error al eliminar: {e}")
                    finally:
                        conn_del.close()
        else:
            st.caption("No hay especialidades para eliminar.")

    # --- 1. Formulario de Inyección de Temas ---
    st.subheader("💉 Inyectar Nuevo Tema para Generación")
    with st.form("matrix_injection_form", clear_on_submit=True):
        topic_name = st.text_input("Nombre del Tema (Ej: 'Fisiología Renal')").strip()
        
        # Campo para seleccionar la categoría de destino
        target_category = st.selectbox(
            "Categoría de Destino",
            options=get_all_categories(), # Usar la función helper
            index=None,
            placeholder="Selecciona la categoría principal para la pregunta"
        )

        # Mapeo de Opciones a Valores de Prioridad
        priority_options = {'Alta/Crítica (1)': 1, 'Normal (3)': 3}
        selected_priority_label = st.selectbox("Prioridad", options=list(priority_options.keys()))
        
        submitted = st.form_submit_button("Añadir a la Cola")
        
        if submitted:
            if not topic_name or not target_category:
                st.warning("El nombre del tema y la categoría de destino no pueden estar vacíos.")
            else:
                priority_value = priority_options[selected_priority_label]
                conn = None
                try:
                    conn = get_db_conn()
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO matrix_topics (topic_name, priority, target_category, created_at) VALUES (?, ?, ?, ?)",
                        (topic_name, priority_value, target_category, datetime.datetime.now())
                    )
                    conn.commit()
                    st.success(f"¡Tema '{topic_name}' añadido a la cola para la categoría '{target_category}'!")
                except sqlite3.IntegrityError:
                    st.warning(f"El tema '{topic_name}' ya existe en la cola.")
                except Exception as e:
                    st.error(f"Error inesperado al añadir el tema: {e}")
                finally:
                    if conn:
                        conn.close()

    st.markdown("---")

    # --- 2. Monitor de Cola de Procesamiento ---
    st.subheader("⏳ Cola de Procesamiento de Temas")

    # Inicializar estado de edición si no existe
    if 'editing_matrix_id' not in st.session_state:
        st.session_state.editing_matrix_id = None

    # Diccionario y función de normalización
    ACRONYM_DICT = {
        "FA": "Fibrilación Auricular", "TEP": "Tromboembolismo Pulmonar",
        "ICC": "Insuficiencia Cardíaca Congestiva", "EPOC": "Enfermedad Pulmonar Obstructiva Crónica",
        "IAM": "Infarto Agudo de Miocardio", "ACV": "Accidente Cerebrovascular",
        "ITU": "Infección del Tracto Urinario", "ERC": "Enfermedad Renal Crónica",
        "DM": "Diabetes Mellitus", "HTA": "Hipertensión Arterial",
        "TB": "Tuberculosis", "VIH": "Virus de Inmunodeficiencia Humana",
        "SCA": "Síndrome Coronario Agudo", "EPID": "Enfermedad Pulmonar Intersticial Difusa",
        "TVP": "Trombosis Venosa Profunda"
    }

    def normalize_text(text):
        for acronym, full in ACRONYM_DICT.items():
            # Reemplazo de palabra completa para evitar falsos positivos
            text = re.sub(r'\b' + re.escape(acronym) + r'\b', full, text)
        return text

    # Botón de Limpieza Global
    col_clean_1, col_clean_2 = st.columns(2)
    
    with col_clean_1:
        if st.button("🧹 Limpiar Temas Completados"):
            conn_clean = get_db_conn()
            try:
                conn_clean.execute("DELETE FROM matrix_topics WHERE status = 'COMPLETADO'")
                conn_clean.commit()
                st.toast("Se eliminaron los temas completados.")
                st.rerun()
            except Exception as e:
                st.error(f"Error al limpiar temas: {e}")
            finally:
                if conn_clean:
                    conn_clean.close()

    with col_clean_2:
        if st.button("🗑️ Eliminar Todo (Pendientes y Completados)", type="primary"):
            st.session_state.confirm_nuke_matrix = True

    if st.session_state.get('confirm_nuke_matrix'):
        st.warning("⚠️ ¿Estás seguro de eliminar TODO?")
        col_conf_yes, col_conf_no = st.columns(2)
        if col_conf_yes.button("Sí, eliminar todo"):
            conn_nuke = get_db_conn()
            try:
                conn_nuke.execute("DELETE FROM matrix_topics")
                conn_nuke.commit()
                st.success("Cola vaciada.")
                st.session_state.confirm_nuke_matrix = False
                st.rerun()
            finally:
                conn_nuke.close()
        
        if col_conf_no.button("Cancelar"):
            st.session_state.confirm_nuke_matrix = False
            st.rerun()

    conn = get_db_conn()
    try:
        # Query para traer los datos
        queue_df = pd.read_sql_query(
            "SELECT id, topic_name, status, priority FROM matrix_topics ORDER BY priority ASC, status ASC, created_at ASC",
            conn
        )

        if queue_df.empty:
            st.info("La cola de generación de temas está vacía.")
        else:
            # Encabezados de la lista
            colh1, colh2, colh3, colh4 = st.columns([3, 2, 2, 1.5])
            with colh1:
                st.markdown("**Tema**")
            with colh2:
                st.markdown("**Prioridad**")
            with colh3:
                st.markdown("**Estado**")
            with colh4:
                st.markdown("**Acción**")
            st.divider()

            # Bucle para mostrar cada tema con su botón
            for index, row in queue_df.iterrows():
                # Modo Edición
                if st.session_state.editing_matrix_id == row['id']:
                    with st.container(border=True):
                        c_edit_1, c_edit_2 = st.columns([3, 1])
                        input_key = f"edit_input_{row['id']}"
                        
                        with c_edit_1:
                            new_name = st.text_input("Editar Tema", value=row['topic_name'], key=input_key, label_visibility="collapsed")
                        
                        with c_edit_2:
                            if st.button("✨ Normalizar", key=f"norm_{row['id']}", help="Expande siglas (ej: FA -> Fibrilación Auricular)"):
                                normalized = normalize_text(new_name)
                                st.session_state[input_key] = normalized
                                st.rerun()

                        c_save, c_cancel = st.columns(2)
                        if c_save.button("💾 Guardar Cambios", key=f"save_{row['id']}", type="primary"):
                            conn_update = get_db_conn()
                            try:
                                conn_update.execute("UPDATE matrix_topics SET topic_name = ? WHERE id = ?", (new_name, row['id']))
                                conn_update.commit()
                                st.success("Tema actualizado.")
                                st.session_state.editing_matrix_id = None
                                st.rerun()
                            finally:
                                conn_update.close()
                        
                        if c_cancel.button("❌ Cancelar", key=f"cancel_{row['id']}"):
                            st.session_state.editing_matrix_id = None
                            st.rerun()
                
                # Modo Visualización
                else:
                    col1, col2, col3, col4 = st.columns([3, 2, 2, 1.5])
                    
                    with col1:
                        st.write(row['topic_name'])
                    
                    with col2:
                        prio_emoji = "🚨" if row['priority'] == 1 else "🛡️"
                        st.write(f"{prio_emoji} Prioridad {row['priority']}")

                    with col3:
                        status = row['status']
                        if status == 'COMPLETADO':
                            st.success("✅ Completado")
                        elif status == 'PENDIENTE':
                            st.info("⏳ Pendiente")
                        elif status == 'PROCESANDO':
                            st.write("⚙️ Procesando...")
                        elif status == 'ERROR':
                            st.error("❌ Error")
                        else:
                            st.write(status)

                    with col4:
                        b_edit, b_del = st.columns(2)
                        with b_edit:
                            if st.button("📝", key=f"edit_btn_{row['id']}", help="Editar"):
                                st.session_state.editing_matrix_id = row['id']
                                st.rerun()
                        with b_del:
                            if st.button("🗑️", key=f"delete_topic_{row['id']}", help="Eliminar"):
                                conn_del = get_db_conn()
                                try:
                                    conn_del.execute("DELETE FROM matrix_topics WHERE id = ?", (row['id'],))
                                    conn_del.commit()
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error al eliminar tema ID {row['id']}: {e}")
                                finally:
                                    if conn_del:
                                        conn_del.close()

    except Exception as e:
        st.error(f"Error al cargar la cola de temas: {e}")
    finally:
        if conn:
            conn.close()
    
    st.markdown("---")

    # --- 3. Refuerzo Adaptativo ---
    st.subheader("🧠 Refuerzo Adaptativo")

    conn_users = get_db_conn()
    try:
        # Traemos solo usuarios que no son admin para el análisis
        users_df = pd.read_sql_query("SELECT username FROM users WHERE role = 'user' ORDER BY username ASC", conn_users)
        user_list = users_df['username'].tolist()
    except Exception as e:
        st.error(f"No se pudieron cargar los usuarios: {e}")
        user_list = []
    finally:
        conn_users.close()

    if not user_list:
        st.info("No hay usuarios para analizar.")
    else:
        selected_user = st.selectbox("Selecciona un usuario para analizar sus debilidades:", user_list)

        if st.button("Analizar Fallos y Generar Refuerzo"):
            if selected_user:
                conn_analysis = get_db_conn()
                try:
                    # Contar los temas (tag_tema) donde el usuario tiene más fallos.
                    query = """
                        SELECT
                            q.tag_tema,
                            SUM(p.fallos) as total_fallos
                        FROM
                            progress p
                        JOIN
                            questions q ON p.question_id = q.id
                        WHERE
                            p.username = ? AND p.fallos > 0 AND q.tag_tema IS NOT NULL AND q.tag_tema != ''
                        GROUP BY
                            q.tag_tema
                        ORDER BY
                            total_fallos DESC
                        LIMIT 3;
                    """
                    top_failed_tags = conn_analysis.execute(query, (selected_user,)).fetchall()

                    if not top_failed_tags:
                        st.info(f"El usuario '{selected_user}' no tiene fallos registrados con temas etiquetados.")
                    else:
                        reinforced_topics = []
                        for row in top_failed_tags:
                            topic_name = row['tag_tema']
                            reinforced_topics.append(topic_name)
                            
                            # Insertar o actualizar el tema en matrix_topics con prioridad crítica.
                            # Usamos ON CONFLICT para actualizar la prioridad si el tema ya existe.
                            conn_analysis.execute("""
                                INSERT INTO matrix_topics (topic_name, priority, status, created_at)
                                VALUES (?, 1, 'PENDIENTE', ?)
                                ON CONFLICT(topic_name) DO UPDATE SET
                                    priority = 1,
                                    status = 'PENDIENTE'
                            """, (topic_name, datetime.datetime.now()))
                        
                        conn_analysis.commit()
                        st.success(f"¡Refuerzo activado para '{selected_user}'! Se han enviado los siguientes temas a La Matriz con prioridad alta: {', '.join(reinforced_topics)}")

                except Exception as e:
                    st.error(f"Ocurrió un error durante el análisis: {e}")
                finally:
                    conn_analysis.close()

class PredictionEngine:
    def __init__(self, current_user_stats):
        # current_user_stats espera: {'precision': float, 'velocidad': float}
        self.user = current_user_stats
        self.ghost = get_ghost_profile()
        
    def calculate_gap(self):
        # Calcula la distancia contra el Fantasma
        if not self.ghost:
            return None 
            
        # 1. Obtener datos del Fantasma (con fallbacks seguros)
        ghost_acc = float(self.ghost.get('final_accuracy_snapshot', 0) or 80.0)
        ghost_speed = float(self.ghost.get('avg_seconds_per_question', 0) or 30.0)
        
        if ghost_speed == 0: ghost_speed = 30.0 # Evitar división por cero
        
        # 2. Comparar
        # Gap de Precisión: (Tu 70% - Ghost 80% = -10)
        acc_gap = self.user.get('precision', 0) - ghost_acc
        
        # Ratio de Velocidad: (Ghost 20s / Tu 40s = 0.5 -> Vas a la mitad de velocidad)
        user_speed = self.user.get('velocidad', 0)
        speed_ratio = ghost_speed / user_speed if user_speed > 0 else 0
        
        return {
            "accuracy_gap": acc_gap, 
            "speed_ratio": speed_ratio,
            "ghost_specialty": self.ghost.get('admitted_specialty', 'General')
        }

def show_admin_panel():
    """Página de gestión de usuarios, moderación, backups y logs."""
    if st.session_state.user_role != 'admin':
        st.error("Acceso denegado."); return
    
    st.header("🔑 Panel de Admin")

    # Initialize session state for confirmations
    if 'admin_pending_action' not in st.session_state:
        st.session_state.admin_pending_action = None
    if 'execution_pending_user' not in st.session_state:
        st.session_state.execution_pending_user = None

    conn = get_db_conn()

    tab_users, tab_observatory, tab_matrix, tab_system, tab_stress = st.tabs(["👥 Gestión de Usuarios", "🔭 Observatorio", "🧬 La Matriz", "📦 Sistema", "🧪 Test de Estrés"])

    with tab_users:
        st.markdown("### 👥 Gestión de Accesos")
        
        try:
            admin_user = st.secrets["ADMIN_USER"]
        except (KeyError, FileNotFoundError):
            admin_user = "admin" 
        
        # --- SECCIÓN NUEVA: USUARIOS PENDIENTES ---
        with st.expander("⏳ Usuarios Pendientes de Aprobación", expanded=True):
            # Consulta adaptada al esquema real: is_approved = 0
            pendientes = conn.execute("SELECT username FROM users WHERE is_approved = 0 AND status = 'active'").fetchall()
            
            if not pendientes:
                st.write("No hay usuarios esperando aprobación. ✅")
            else:
                for p in pendientes:
                    col1, col2, col3 = st.columns([2, 1, 1])
                    col1.write(f"**{p['username']}**")
                    # Configurador de Días de Acceso
                    days_grant = col2.number_input("Días", value=30, min_value=1, key=f"days_{p['username']}", label_visibility="collapsed")
                    if col3.button("✅ Aprobar", key=f"quick_approve_{p['username']}"):
                        exp_date = datetime.date.today() + datetime.timedelta(days=days_grant)
                        conn.execute("UPDATE users SET is_approved = 1, access_expiration = ? WHERE username = ?", (exp_date, p['username']))
                        conn.commit()
                        st.success(f"Usuario {p['username']} aprobado hasta {exp_date}")
                        st.rerun()
        # ------------------------------------------

        # --- 1. SECCIÓN: GESTIONAR USUARIOS ACTIVOS ---
        
        # Filtramos admin_user Y el usuario del sistema 'Matrix_AI'
        usuarios_activos = conn.execute(
            "SELECT username, role, is_approved, is_intensive, max_inactivity_days, status, is_reference_model, admitted_status, admitted_specialty, final_accuracy_snapshot, avg_daily_questions, avg_seconds_per_question, total_questions_snapshot, access_expiration FROM users WHERE username != ? AND username != 'Matrix_AI' AND status = 'active'", 
            (admin_user,)
        ).fetchall()

        if not usuarios_activos:
            st.info("No hay usuarios activos para gestionar.")
        else:
            with st.expander("📂 Ver / Buscar Usuarios Activos", expanded=True):
                search_query = st.text_input("🔍 Buscar por nombre de usuario:", "").lower().strip()
                
                if search_query:
                    filtered_users = [u for u in usuarios_activos if search_query in u['username'].lower()]
                else:
                    filtered_users = usuarios_activos
                    
                if search_query:
                    st.caption(f"Encontrados: {len(filtered_users)} de {len(usuarios_activos)}")
                
                if filtered_users:
                    for user_row in filtered_users:
                        username = user_row['username']
                        is_approved = user_row['is_approved']
                        
                        st.markdown("---")
                        col1, col2, col3 = st.columns([2, 1, 1.5])
                        
                        with col1:
                            st.markdown(f"**{username}** ({user_row['role']})")
                            status_text = "🔥 Activo" if user_row['is_intensive'] else "Inactivo"
                            st.caption(f"Modo Intensivo: {status_text}")
                            if user_row['access_expiration']:
                                st.caption(f"📅 Vence: {user_row['access_expiration']}")

                        with col2:
                            st.write("✅ Aprobado" if user_row['is_approved'] else "⏳ Pendiente")

                        with col3:
                            pending_action = st.session_state.admin_pending_action
                            if pending_action and pending_action['username'] == username:
                                action_text_map = {'aprobar': 'aprobar', 'revocar': 'revocar la aprobación', 'eliminar': 'eliminar'}
                                action_text = action_text_map.get(pending_action['action'], 'realizar esta acción')
                                st.warning(f"¿Seguro que deseas {action_text} a **{username}**?")
                                
                                confirm_col, cancel_col = st.columns(2)
                                if confirm_col.button("✅ Sí, confirmar", key=f"confirm_{username}", type="primary"):
                                    action = pending_action['action']
                                    if action == 'aprobar':
                                        conn.execute("UPDATE users SET is_approved = 1 WHERE username = ?", (username,))
                                        conn.commit()
                                        st.success(f"Usuario {username} aprobado.")
                                    elif action == 'revocar':
                                        conn.execute("UPDATE users SET is_approved = 0 WHERE username = ?", (username,))
                                        conn.commit()
                                        st.success(f"Aprobación de {username} revocada.")
                                    elif action == 'eliminar':
                                        delete_user_from_db(username)
                                    
                                    st.session_state.admin_pending_action = None
                                    st.rerun()

                                if cancel_col.button("❌ Cancelar", key=f"cancel_{username}"):
                                    st.session_state.admin_pending_action = None
                                    st.rerun()
                            else:
                                if is_approved == 0:
                                    if st.button("Aprobar", key=f"approve_{username}"):
                                        st.session_state.admin_pending_action = {'username': username, 'action': 'aprobar'}
                                        st.rerun()
                                else:
                                    if st.button("Revocar", key=f"revoke_{username}", type="secondary"):
                                        st.session_state.admin_pending_action = {'username': username, 'action': 'revocar'}
                                        st.rerun()
                                if st.button("Eliminar ⚠️", key=f"del_{username}"):
                                    st.session_state.admin_pending_action = {'username': username, 'action': 'eliminar'}
                                    st.rerun()

                        # --- NUEVO CONTROL DE VIGENCIA VERSÁTIL (ACTUALIZADO) ---
                        with st.container(border=True):
                            st.write("🗓️ **Control de Acceso y Vigencia**")
                            
                            # Determinar si es actualmente indefinido (Adaptado a access_expiration)
                            exp_date_str = user_row['access_expiration']
                            val_actual = 30
                            is_indefinite_db = False
                            
                            if exp_date_str:
                                if exp_date_str == '9999-12-31':
                                    is_indefinite_db = True
                                    val_actual = 9999
                                else:
                                    try:
                                        exp_date_obj = datetime.datetime.strptime(exp_date_str, '%Y-%m-%d').date()
                                        delta = (exp_date_obj - datetime.date.today()).days
                                        val_actual = max(0, delta)
                                    except:
                                        pass

                            is_inf = st.checkbox("Acceso Indefinido (Sin límite de días) ♾️", value=is_indefinite_db, key=f"inf_{username}")
                            
                            if is_inf:
                                nuevos_dias = 9999
                                st.caption("Acceso total habilitado.")
                            else:
                                default_val = val_actual if val_actual < 9999 else 30
                                nuevos_dias = st.number_input("Días autorizados:", 1, 3650, value=default_val, key=f"days_{username}")
                            
                            if st.button("Actualizar Vigencia", key=f"btn_v_{username}"):
                                new_exp_date_str = '9999-12-31' if is_inf else (datetime.date.today() + datetime.timedelta(days=nuevos_dias)).strftime('%Y-%m-%d')
                                conn.execute("UPDATE users SET access_expiration = ? WHERE username = ?", (new_exp_date_str, username))
                                conn.commit()
                                st.success(f"Vigencia guardada: {'Indefinido' if is_inf else str(nuevos_dias) + ' días'}")
                                st.rerun()
                        # --- FIN CONTROL DE VIGENCIA ---

                        with st.expander('⚙️ Configurar Modo Intensivo'):
                            with st.form(key=f"intensive_form_{username}"):
                                intensive_active = st.checkbox('Activar Modo Intensivo', value=bool(user_row['is_intensive']))
                                inactivity_days = st.number_input('Días Máximos de Inactividad', min_value=1, max_value=30, value=user_row['max_inactivity_days'])
                                
                                if st.form_submit_button('Guardar Configuración'):
                                    new_is_intensive = 1 if intensive_active else 0
                                    
                                    if new_is_intensive == 1 and not user_row['is_intensive']:
                                        start_date = datetime.date.today()
                                        conn.execute("UPDATE users SET is_intensive = ?, max_inactivity_days = ?, intensive_start_date = ? WHERE username = ?", (new_is_intensive, inactivity_days, start_date, username))
                                    elif new_is_intensive == 0 and user_row['is_intensive']:
                                        conn.execute("UPDATE users SET is_intensive = ?, intensive_start_date = NULL WHERE username = ?", (new_is_intensive, username))
                                    else:
                                        conn.execute("UPDATE users SET max_inactivity_days = ? WHERE username = ?", (inactivity_days, username))
                                    
                                    conn.commit()
                                    st.success(f"Configuración de Modo Intensivo guardada para {username}.")
                                    st.rerun()

                        with st.expander('👻 Configuración de Modelo / Fantasma'):
                            with st.form(key=f"ghost_form_{username}"):
                                st.markdown("##### 🧬 Perfil del Experto (Reference Model)")
                                c1, c2, c3 = st.columns(3)
                                new_is_ref = c1.checkbox("Es Modelo Referencia", value=bool(user_row['is_reference_model']), key=f"ref_{user_row['username']}")
                                current_status = user_row['admitted_status'] if user_row['admitted_status'] in ["No Admitido", "Admitido", "Pending"] else "Pending"
                                status_opts = ["Pending", "No Admitido", "Admitido"]
                                new_status = c2.selectbox("Estatus", status_opts, index=status_opts.index(current_status), key=f"stat_{user_row['username']}")
                                new_specialty = c3.text_input("Especialidad Objetivo/Lograda", value=user_row['admitted_specialty'] or "", key=f"spec_{user_row['username']}")
                                st.divider()
                                st.caption("📊 Métricas de Hábito (Se llenarán automáticamente tras el estudio o puedes editar):")
                                c4, c5 = st.columns(2)
                                new_acc = c4.number_input("Precisión Global (%)", value=float(user_row['final_accuracy_snapshot'] or 0.0), key=f"acc_{user_row['username']}")
                                new_speed = c5.number_input("Velocidad (Seg/Pregunta)", value=float(user_row['avg_seconds_per_question'] or 0.0), key=f"spd_{user_row['username']}")
                                c6, c7 = st.columns(2)
                                new_daily = c6.number_input("Promedio Diario (Preg/Día)", value=float(user_row['avg_daily_questions'] or 0.0), key=f"dly_{user_row['username']}")
                                new_total = c7.number_input("Total Histórico", value=int(user_row['total_questions_snapshot'] or 0), key=f"tot_{user_row['username']}")

                                if st.form_submit_button('Guardar Rol Fantasma'):
                                    conn.execute(
                                        """UPDATE users SET 
                                            is_reference_model=?, admitted_status=?, admitted_specialty=?, 
                                            final_accuracy_snapshot=?, avg_daily_questions=?, avg_seconds_per_question=?, 
                                            total_questions_snapshot=? 
                                           WHERE username=?""",
                                        (1 if new_is_ref else 0, new_status, new_specialty, new_acc, new_daily, new_speed, new_total, username)
                                    )
                                    conn.commit()
                                    st.success(f"Configuración de Modelo de Referencia guardada para {username}.")
                                    st.rerun()
                else:
                    st.info(f"🚫 No se encontraron usuarios que coincidan con '{search_query}'.")

        # --- 2. SECCIÓN: ZONA DE JUICIO ---
        st.markdown("---")
        pending_deletion_users = conn.execute("SELECT * FROM users WHERE status = 'pending_delete'").fetchall()

        with st.expander("💀 Zona de Juicio (Pendientes de Eliminación)", expanded=False):
            if not pending_deletion_users:
                st.info("No hay usuarios pendientes de eliminación.")
            else:
                search_juicio = st.text_input("🔍 Buscar condenado:", "", key="search_juicio").lower()
                
                filtered_pending = [u for u in pending_deletion_users if search_juicio in u['username'].lower()]
                
                if filtered_pending:
                    for user_row in filtered_pending:
                        username = user_row['username']
                        st.markdown("---")
                        
                        score, _, _ = calculate_user_score(username, user_row['max_inactivity_days'])
                        reason = f"Puntaje de productividad bajo ({score}/30)"
                        
                        container = st.container(border=True)
                        container.error(f"**Usuario:** {username}\n\n**Motivo:** {reason}")
                        
                        if st.session_state.execution_pending_user == username:
                            container.warning(f"¿Seguro que deseas ELIMINAR PERMANENTEMENTE a {username}?")
                            exec_col, cancel_exec_col = container.columns(2)
                            
                            if exec_col.button("✅ Sí, ejecutar", key=f"exec_confirm_{username}", type="primary"):
                                conn.execute("INSERT INTO deleted_users_log (username, deletion_date, reason) VALUES (?, ?, ?)", (username, datetime.datetime.now(), reason))
                                conn.commit()
                                delete_user_from_db(username)
                                st.session_state.execution_pending_user = None
                                st.success(f"El usuario {username} ha sido ejecutado.")
                                st.rerun()

                            if cancel_exec_col.button("❌ No, cancelar ejecución", key=f"exec_cancel_{username}"):
                                st.session_state.execution_pending_user = None
                                st.rerun()
                        else:
                            pardon_col, execute_col = container.columns(2)
                            if pardon_col.button("Indultar (Perdonar)", key=f"pardon_{username}"):
                                conn.execute("UPDATE users SET status = 'active' WHERE username = ?", (username,))
                                conn.execute("INSERT INTO activity_log (username, action_type, timestamp) VALUES (?, 'pardoned', ?)", (username, datetime.datetime.now()))
                                conn.commit()
                                st.success(f"{username} ha sido indultado y su cuenta ha sido reactivada.")
                                st.rerun()

                            if execute_col.button("Ejecutar (Eliminar)", key=f"execute_{username}", type="primary"):
                                st.session_state.execution_pending_user = username
                                st.rerun()
                else:
                    st.warning("No se encontraron coincidencias.")

        # --- 3. SECCIÓN: HISTORIAL DE ELIMINADOS ---
        st.markdown("---")
        deleted_log_df = pd.read_sql_query("SELECT username, deletion_date, reason FROM deleted_users_log ORDER BY deletion_date DESC", conn)

        with st.expander("🪵 Historial de Eliminados (Cementerio)", expanded=False):
            if deleted_log_df.empty:
                st.info("El cementerio está vacío.")
            else:
                search_hist = st.text_input("🔍 Buscar en historial:", "", key="search_hist")
                
                if search_hist:
                    try:
                        filtered_df = deleted_log_df[deleted_log_df['username'].astype(str).str.contains(search_hist, case=False, na=False)]
                        st.dataframe(filtered_df, use_container_width=True)
                    except:
                        st.dataframe(deleted_log_df, use_container_width=True)
                else:
                    st.dataframe(deleted_log_df, use_container_width=True)

    with tab_observatory:
        st.markdown("## 🔭 Observatorio de Rendimiento (Consultoría)")
    
        # Selector Dinámico: Busca en logs para ver invitados como 'Guest_Ganador_Previo'
        users_list_df = pd.read_sql_query("SELECT DISTINCT username FROM activity_log ORDER BY username", conn)
        all_users = users_list_df['username'].tolist()
        
        # Filtrar cuentas técnicas o irrelevantes
        all_users = [u for u in all_users if u not in ['guest_mode', 'usuario_test']]
        
        if all_users:
            tgt_user = st.selectbox("Seleccionar Usuario a Espiar:", all_users, index=0, key="observatory_user_select")
            
            df_analytics = get_user_analytics(tgt_user)
            
            if not df_analytics.empty:
                kpi1, kpi2, kpi3 = st.columns(3)
                avg_speed = df_analytics['Velocidad (s)'].mean()
                accuracy = (df_analytics['Resultado'] == 'correct').mean() * 100
                total_q = len(df_analytics)
                
                kpi1.metric("Velocidad Promedio", f"{avg_speed:.2f} s")
                kpi2.metric("Precisión Actual", f"{accuracy:.1f} %")
                kpi3.metric("Preguntas Analizadas", f"{total_q}")
                
                st.divider()
                st.markdown("#### 🧠 Análisis vs. Fantasma")
                
                current_stats = {'precision': float(accuracy), 'velocidad': float(avg_speed)}
                engine = PredictionEngine(current_stats)
                gaps = engine.calculate_gap()
                
                if gaps:
                    c_ghost1, c_ghost2, c_ghost3 = st.columns(3)
                    gap_acc = gaps['accuracy_gap']
                    c_ghost1.metric("Brecha de Precisión", f"{gap_acc:.1f}%", delta=f"{gap_acc:.1f}%", delta_color="normal")
                    speed_pct = gaps['speed_ratio'] * 100
                    c_ghost2.metric("Ritmo vs Fantasma", f"{speed_pct:.0f}%", delta=f"{speed_pct - 100:.0f}% (Más lento)" if speed_pct < 100 else "Más rápido", delta_color="normal")
                    c_ghost3.info(f"Comparando contra: **{gaps['ghost_specialty']}**")
                else:
                    st.warning("⚠️ No se ha configurado un Usuario Fantasma (Referencia) en la BD.")

                st.caption("📈 Evolución de Velocidad (Segundos por Pregunta)")
                st.line_chart(df_analytics.set_index('Fecha')['Velocidad (s)'])
                
                st.caption("🎯 Distribución de Resultados")
                res_counts = df_analytics['Resultado'].value_counts()
                st.bar_chart(res_counts)
                
                with st.expander("Ver Datos Crudos"):
                    st.dataframe(df_analytics)

                st.divider()
                st.markdown("#### 🧬 ADN Temático: Tú vs. El Fantasma")
                
                ghost_profile = get_ghost_profile()
                
                if ghost_profile:
                    df_ghost = get_user_analytics(ghost_profile['username'])
                    
                    if not df_ghost.empty and not df_analytics.empty:
                        user_topic_acc = df_analytics.groupby('Tema').apply(lambda x: (x['Resultado'] == 'correct').mean() * 100).rename("Usuario")
                        ghost_topic_acc = df_ghost.groupby('Tema').apply(lambda x: (x['Resultado'] == 'correct').mean() * 100).rename("Fantasma")
                        comparison_df = pd.concat([user_topic_acc, ghost_topic_acc], axis=1).fillna(0)
                        st.bar_chart(comparison_df)
                        comparison_df['Brecha'] = comparison_df['Usuario'] - comparison_df['Fantasma']
                        critical = comparison_df[(comparison_df['Fantasma'] > 60) & (comparison_df['Brecha'] < -20)]
                        
                        if not critical.empty:
                            st.error("🚨 ALERTA: El Fantasma domina estos temas y tú no:")
                            for topic, row in critical.iterrows():
                                diff = abs(row['Brecha'])
                                st.write(f"- **{topic}**: Estás {diff:.1f}% por debajo del nivel de referencia.")
                    else:
                        st.info("Aún no hay suficientes datos coincidentes entre ambos usuarios para comparar temas.")
                else:
                    st.warning("⚠️ No hay Fantasma configurado.")
            else:
                st.info(f"El usuario {tgt_user} aún no tiene telemetría registrada (Eventos 'answer_submitted').")
        else:
            st.warning("No hay usuarios en la base de datos.")

    with tab_matrix:
        render_matrix_admin()

    with tab_system:
        st.subheader("📦 Copia de Seguridad (Backup)")

        try:
            with open(dbm.DB_PATH, "rb") as fp:
                st.download_button(
                    label="Descargar Base de Datos (SQLite)",
                    data=fp,
                    file_name=f"backup_prisma_srs_{datetime.date.today().strftime('%Y-%m-%d')}.db",
                    mime="application/x-sqlite3"
                )
            st.info("Este archivo contiene todos los datos de usuarios y preguntas. Guárdalo en un lugar seguro.")
        except FileNotFoundError:
            st.error(f"Error: No se encontró el archivo de la base de datos en la ruta: {dbm.DB_PATH}")
        except Exception as e:
            st.error(f"Ocurrió un error inesperado al leer el archivo de la base de datos: {e}")
        
        st.markdown("---")
        st.subheader("📊 Exportar Data para Análisis")

        @st.cache_data
        def generate_excel_export():
            output = io.BytesIO()
            conn_export = get_db_conn()
            try:
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df_users = pd.read_sql_query("SELECT * FROM users", conn_export)
                    if 'password_hash' in df_users.columns:
                        df_users = df_users.drop(columns=['password_hash'])
                    df_users.to_excel(writer, sheet_name='Usuarios', index=False)

                    df_logs = pd.read_sql_query("SELECT * FROM activity_log", conn_export)

                    if not df_logs.empty and 'metadata' in df_logs.columns:
                        def safe_json_load(x):
                            try:
                                if x and isinstance(x, str):
                                    return json.loads(x)
                            except (json.JSONDecodeError, TypeError):
                                pass
                            return {}

                        df_meta = pd.json_normalize(df_logs['metadata'].apply(safe_json_load))
                        df_logs = df_logs.join(df_meta)
                        rename_map = {'time_seconds': 'Velocidad (s)', 'topic': 'Tema', 'result': 'Resultado', 'difficulty_rating': 'Dificultad'}
                        existing_renames = {k: v for k, v in rename_map.items() if k in df_logs.columns}
                        if existing_renames:
                            df_logs.rename(columns=existing_renames, inplace=True)
                        if 'metadata' in df_logs.columns:
                            df_logs.drop(columns=['metadata'], inplace=True)
                    
                    df_logs.to_excel(writer, sheet_name='Telemetría', index=False)
            finally:
                conn_export.close()
            
            output.seek(0)
            return output.getvalue()

        try:
            excel_data = generate_excel_export()
            
            st.download_button(
                label="Descargar Dataset Completo (.xlsx)",
                data=excel_data,
                file_name=f"dataset_k_community_{datetime.date.today().strftime('%Y-%m-%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        except Exception as e:
            st.error(f"Ocurrió un error al generar el dataset para descarga: {e}")

    with tab_stress:
        st.header("🧪 Test de Estrés MAFU 2.0 (Simulación de Carga)")
        st.info("Esta herramienta audita la integridad del Algoritmo de Selección y la estructura de la Base de Datos.")

        if st.button("▶️ EJECUTAR DIAGNÓSTICO", type="primary"):
            st.markdown("---")
            
            # 1. Simulación de Pesos
            st.subheader("1. Simulación de Pesos (Monte Carlo n=500)")
            topics = list(GOLDEN_RATIO_DETAILED.keys())
            weights = list(GOLDEN_RATIO_DETAILED.values())
            
            simulated_picks = random.choices(topics, weights=weights, k=500)
            counts = pd.Series(simulated_picks).value_counts(normalize=True) * 100
            
            col1, col2 = st.columns(2)
            with col1:
                ped_actual = counts.get('Pediatría', 0.0)
                st.metric("Pediatría (Target ~16%)", f"{ped_actual:.1f}%", delta=f"{ped_actual-16:.1f}%")
            with col2:
                oft_actual = counts.get('Oftalmología', 0.0)
                st.metric("Oftalmología (Target ~3%)", f"{oft_actual:.1f}%", delta=f"{oft_actual-3:.1f}%")
            
            st.bar_chart(counts)
            
            if 10 <= ped_actual <= 22: 
                st.success("✅ PASSED: Distribución estadística coherente.")
            else:
                st.warning("⚠️ WARNING: La distribución presenta desviaciones (Normal en muestras pequeñas, revisar si persiste).")

            st.markdown("---")

            # 2. Prueba de Cero Huérfanas
            st.subheader("2. Prueba de 'Cero Huérfanas'")
            valid_tags = list(GOLDEN_RATIO_DETAILED.keys())
            placeholders = ','.join(['?'] * len(valid_tags))
            
            try:
                query = f"SELECT tag_categoria, COUNT(*) as count FROM questions WHERE tag_categoria NOT IN ({placeholders}) AND status='active' AND tag_categoria IS NOT NULL GROUP BY tag_categoria"
                orphans = pd.read_sql_query(query, conn, params=valid_tags)
                
                if orphans.empty:
                    st.success("✅ PASSED: Integridad total. No existen categorías fuera del estándar MAFU 2.0.")
                else:
                    st.error("❌ FAILED: Se detectaron categorías huérfanas o antiguas:")
                    st.dataframe(orphans)
            except Exception as e:
                st.error(f"Error en consulta SQL: {e}")
            
            st.markdown("---")

            # 3. Verificación de Búsqueda Exacta
            st.subheader("3. Verificación de Búsqueda Exacta (Sintaxis SQL)")
            complex_topics = ["Ginecología y Obstetricia", "Otorrinolaringología", "Cirugía General"]
            errors = []
            
            for topic in complex_topics:
                try:
                    # Simulamos la query exacta usada en get_next_question_for_user
                    query_fractal = "SELECT id FROM questions WHERE status = 'active' AND (tag_tema LIKE ? OR tag_categoria LIKE ?) LIMIT 1"
                    term_like = f"{topic}%"
                    conn.execute(query_fractal, (term_like, term_like))
                except Exception as e:
                    errors.append(f"{topic}: {e}")
            
            if not errors:
                st.success(f"✅ PASSED: El motor SQL procesa correctamente etiquetas complejas: {', '.join(complex_topics)}.")
            else:
                st.error(f"❌ FAILED: Errores de sintaxis detectados: {errors}")

    conn.close()

def show_change_password_page():
    """Permite al usuario logueado cambiar su propia contraseña."""
    st.subheader("🔐 Cambiar Mi Contraseña")
    with st.form("change_password_form", clear_on_submit=True):
        password_new = st.text_input("Nueva Contraseña", type="password")
        password_confirm = st.text_input("Confirmar Nueva Contraseña", type="password")
        if st.form_submit_button("Actualizar Contraseña"):
            if password_new and password_new == password_confirm:
                password_new_bytes = password_new.encode('utf-8')[:72]
                new_hash = pwd_context.hash(password_new_bytes)
                conn = get_db_conn()
                conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (new_hash, st.session_state.current_user))
                conn.commit(); conn.close()
                st.success("¡Contraseña actualizada con éxito!"); st.balloons()
            else:
                st.error("Las contraseñas no coinciden o están vacías.")

def run_auto_backup():
    """Crea una copia de seguridad de la base de datos en la carpeta de backups."""
    
    # 1. Definir rutas
    # Ruta de la base de datos original (producción o local)
    source_db = dbm.DB_PATH
    
    # Directorio de backups (relativo a la ubicación de app.py)
    backup_dir = "backups" 
    
    # Nombre del archivo de backup con la fecha actual
    backup_filename = f"backup_prisma_srs_{datetime.date.today().strftime('%Y-%m-%d')}.db"
    
    # Ruta completa del destino
    dest_db = os.path.join(backup_dir, backup_filename)
    
    try:
        # 2. Asegurarse de que el directorio de backups existe
        os.makedirs(backup_dir, exist_ok=True)
        
        # 3. Solo copiar si no existe ya un backup para hoy
        if not os.path.exists(dest_db):
            # Copiar el archivo
            shutil.copy2(source_db, dest_db)
            print(f"✅ Backup automático creado con éxito en: {dest_db}")
        else:
            print(f"ℹ️ El backup de hoy ya existe. No se necesita crear uno nuevo.")
            
    except FileNotFoundError:
        print(f"❌ ERROR en backup: No se encontró la base de datos de origen en '{source_db}'.")
    except Exception as e:
        print(f"❌ ERROR inesperado durante el backup automático: {e}")

# --- CONTROLADOR PRINCIPAL (MAIN) ---

def main():
    """Función principal que actúa como enrutador."""
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.current_user = None
        st.session_state.user_role = None
        st.session_state.current_page = "login"

    if not st.session_state.logged_in:
        col1, col2 = st.columns(2)
        
        with col1:
            show_login_page()
            
        with col2:
            st.markdown("<br><br>", unsafe_allow_html=True) # Ajuste visual para centrar verticalmente
            with st.container(border=True):
                st.subheader("🎓 Modo Entrenamiento Gratuito")
                st.markdown("¿Eres nuevo? Prueba la plataforma sin registrarte.")
                st.markdown("- 🧠 **Motor FSRS** activo.\n- 🏥 **Preguntas Reales**.\n- ⏳ **Acceso Limitado**.")
                
                if st.button("🚀 Entrar como Invitado", use_container_width=True):
                    st.session_state.logged_in = True
                    st.session_state.current_user = 'guest_mode'
                    st.session_state.user_role = 'guest'
                    st.rerun()
    else:
        # --- GATEKEEPER: MODO INVITADO ---
        if st.session_state.user_role == 'guest':
            can_play, msg, needs_survey = check_guest_access()
            
            # Bloqueo 1: Encuesta Obligatoria
            if needs_survey:
                st.title("📋 Paso Final: Encuesta de Acceso")
                with st.form("guest_survey"):
                    st.write("Para habilitar tu prueba gratuita, necesitamos conocer tu perfil.")
                    
                    # 1. Nuevos Textos (Pregunta y Opciones)
                    is_resident = st.radio(
                        "¿Ya has pasado residencia médica?", 
                        ["No, nunca he pasado examenes de residencia", "Sí, ya he pasado examenes de residencia"]
                    )
                    
                    attempts = st.number_input("¿Cuántas veces has presentado el examen?", min_value=0, max_value=20, value=0)
                    
                    # 2. Botón y Validación Lógica Corregida
                    if st.form_submit_button("Activar Modo Invitado"):
                        # CRÍTICO: La comparación coincide carácter por carácter con la opción 'Sí'
                        admitted = (is_resident == "Sí, ya he pasado examenes de residencia")
                        # GUARDAR EN SESIÓN PARA MINERÍA
                        st.session_state['guest_profile_passed'] = admitted 
                        register_guest_survey(admitted, attempts)
                        st.success("¡Perfil guardado! Iniciando...")
                        time.sleep(1)  # Pequeña pausa para UX
                        st.rerun()
                st.stop()
            
            # Bloqueo 2: Paywall (Límite Diario)
            if not can_play:
                # --- NUEVO DISEÑO DE PAYWALL PROFESIONAL ---
                st.error("🚫 Límite diario alcanzado (5/5). Inicia sesión para continuar.")
                st.markdown("### 🔓 Has alcanzado el límite diario de invitados")
                st.info("Para seguir entrenando con el algoritmo MAFU y acceder a preguntas ilimitadas, adquiere tu Plan Premium.")
                
                # Mostrar Plan Único
                st.markdown("""
                <div style='background-color: #f0f2f6; padding: 20px; border-radius: 10px; border-left: 5px solid #ff4b4b; margin-bottom: 20px;'>
                    <h2 style='margin: 0;'>🚀 Plan Full Acceso</h2>
                    <p style='font-size: 24px; font-weight: bold; color: #ff4b4b; margin: 10px 0;'>$26.00 USD <span style='font-size: 14px; color: #666;'>/ mes</span></p>
                    <ul style='font-size: 16px;'>
                        <li>✅ Preguntas y casos clínicos ilimitados</li>
                        <li>✅ Algoritmo de repetición espaciada MAFU</li>
                        <li>✅ Estadísticas de rendimiento avanzadas</li>
                        <li>✅ Acceso a Duelos y Biblioteca por temas</li>
                    </ul>
                </div>
                """, unsafe_allow_html=True)
                
                # Botón de Contacto
                st.markdown("#### 📩 ¿Deseas activar tu cuenta?")
                st.write("Para más información sobre medios de pago y activación inmediata, escríbenos:")
                
                st.link_button(
                    "📸 Contactar en Instagram @clubresidentmd", 
                    "https://www.instagram.com/clubresidentmd",
                    type="primary",
                    use_container_width=True
                )
                
                if st.button("⬅️ Volver al Inicio"):
                    st.session_state.logged_in = False
                    st.rerun()
                st.stop()
            
            # Pase Libre
            st.toast(msg, icon="🎟️")
        # --- FIN GATEKEEPER ---

        if st.session_state.user_role == 'guest':
            st.sidebar.title("¡Bienvenido!")
        else:
            st.sidebar.title(f"Bienvenido, {st.session_state.current_user}")
            st.sidebar.caption(f"Rol: {st.session_state.user_role}")

        # --- INICIO SECCIÓN MODO INTENSIVO: Widget de Productividad ---
        show_productivity_widget()
        # --- FIN SECCIÓN MODO INTENSIVO ---

        st.sidebar.markdown("---")
        
        # --- MENÚ LATERAL (SIDEBAR) ---
        
        # 1. Botón Universal (Para todos)
        if st.sidebar.button("🧠 Iniciar Evaluación", use_container_width=True):
            st.session_state.current_page = "evaluacion"; reset_evaluation_state(); st.rerun()

        # 2. Botones EXCLUSIVOS para Usuarios Registrados (No Guests)
        if st.session_state.get('user_role') != 'guest':
            if st.sidebar.button("📚 Biblioteca por Temas", use_container_width=True):
                st.session_state.current_page = "topics"; reset_evaluation_state(); st.rerun()
            if st.sidebar.button("⚔️ Duelos", use_container_width=True):
                st.session_state.current_page = "duelos"; st.rerun()
            if st.sidebar.button("🖊️ Crear Preguntas", use_container_width=True):
                st.session_state.current_page = "crear"; st.rerun()
            if st.sidebar.button("📋 Gestionar Mis Preguntas", use_container_width=True):
                st.session_state.current_page = "gestionar"; st.rerun()
            if st.sidebar.button("📊 Estadísticas y Ranking", use_container_width=True):
                st.session_state.current_page = "estadisticas"; st.rerun()
            
            # --- INICIO: Buzón de Sugerencias (Pre-producción) ---
            with st.sidebar.expander("💡 Sugerir Tema de Estudio"):
                with st.form("suggestion_form", clear_on_submit=True):
                    suggested_topic = st.text_input("Tema sugerido:")
                    if st.form_submit_button("Enviar a Revisión"):
                        if suggested_topic.strip():
                            conn_sugg = get_db_conn()
                            try:
                                conn_sugg.execute(
                                    "INSERT INTO suggested_topics (raw_topic, suggester_name, status) VALUES (?, ?, 'PENDING')",
                                    (suggested_topic.strip(), st.session_state.current_user)
                                )
                                conn_sugg.commit()
                                st.success('✅ Enviado a Pre-producción')
                            except Exception as e:
                                st.error(f"Error: {e}")
                            finally:
                                conn_sugg.close()
                        else:
                            st.warning("Escribe un tema.")
            # --- FIN: Buzón de Sugerencias ---

            # Panel Admin
            if st.session_state.user_role == 'admin':
                st.sidebar.markdown("---"); st.sidebar.markdown("Panel de Administrador")
                if st.sidebar.button("🔑 Gestionar Usuarios", use_container_width=True):
                    st.session_state.current_page = "admin_users"; st.rerun()

        st.sidebar.markdown("---")
        
        # 3. Footer del Menú
        if st.sidebar.button("📜 Reglamento / Ayuda", use_container_width=True):
            st.session_state.current_page = "rules"; st.rerun()
            
        # Cambiar contraseña (SOLO REGISTRADOS)
        if st.session_state.get('user_role') != 'guest':
            if st.sidebar.button("🔐 Cambiar Contraseña", use_container_width=True):
                st.session_state.current_page = "change_password"; st.rerun()
        
        # Botón de Salida (Logout)
        if st.sidebar.button("🚪 Cerrar Sesión", type="primary", use_container_width=True):
            clear_evaluation_memory() # Limpieza total antes de salir
            st.session_state.logged_in = False
            st.session_state.current_user = None
            st.session_state.user_role = None
            # No es necesario limpiar más, st.rerun() cargará la página de login limpia.
            st.rerun()

        page_functions = {
            "evaluacion": show_evaluation_page,
            "topics": show_topics_page,
            "crear": show_create_page,
            "gestionar": show_manage_questions_page,
            "estadisticas": show_stats_page,
            "duelos": show_duels_page,
            "admin_users": show_admin_panel,
            "change_password": show_change_password_page,
            "rules": show_rules_page,
        }
        
        
        # --- ROUTER LOGIC AUDIT: GUEST MODE ---
        if st.session_state.get('user_role') == 'guest':
            # Definir lista blanca de páginas permitidas para invitados
            allowed_guest_pages = ["evaluacion", "rules"]
            current_pg = st.session_state.get("current_page", "evaluacion")
            
            if current_pg not in allowed_guest_pages:
                # Redirección forzosa al Dashboard de Invitado (Evaluación)
                st.session_state.current_page = "evaluacion"
                # Opcional: st.toast("Redirigiendo a zona permitida...")
                st.rerun()

        page_to_show = page_functions.get(st.session_state.get("current_page", "evaluacion"), show_evaluation_page)
        page_to_show()

# --- EJECUCIÓN ---
if __name__ == "__main__":
    # --- INICIO: Ejecución de Tareas de Arranque ---
    if 'backup_done' not in st.session_state:
        run_auto_backup()
        st.session_state.backup_done = True
    # --- FIN: Tareas de Arranque ---
    setup_database()
    main()
