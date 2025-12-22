import streamlit as st
import uuid
import socket
import database_manager as dbm
from passlib.context import CryptContext

# Configuración de Hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_remote_ip():
    """
    Obtiene la IP real del cliente, manejando proxies (Render/Cloudflare).
    """
    try:
        # 1. Intentar obtener desde headers de Streamlit (X-Forwarded-For)
        # Esto es CRÍTICO para Render, si no todos los usuarios parecen tener la misma IP interna.
        if st.context.headers:
             x_forwarded_for = st.context.headers.get("X-Forwarded-For")
             if x_forwarded_for:
                 # Puede ser una lista "client, proxy1, proxy2", tomamos el primero
                 return x_forwarded_for.split(',')[0].strip()
        
        # 2. Fallback para desarrollo local
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "0.0.0.0"

def init_session():
    """
    Inicializa la sesión de forma segura y aislada.
    Garantiza que cada recarga tenga un UUID único si no existe,
    evitando cruce de datos.
    """
    if 'session_id' not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.user_ip = get_remote_ip()
        st.session_state.is_authenticated = False
        st.session_state.current_user = None
        st.session_state.user_role = None
        
        # Inicialización de estado de navegación
        if 'current_page' not in st.session_state:
            st.session_state.current_page = "login"

    # Logging básico (opcional)
    # print(f"Session Init: {st.session_state.session_id} | IP: {st.session_state.user_ip}")

def verify_password(plain_password, hashed_password):
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False

def login_user(username, password):
    """
    Lógica centralizada de login.
    Devuelve (success, role, message)
    """
    conn = dbm.get_db_conn()
    try:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        
        if not user:
            return False, None, "Usuario no encontrado."
            
        if not verify_password(password, user['password_hash']):
            # Protección básica contra fuerza bruta podría ir aquí (contar intentos)
            return False, None, "Contraseña incorrecta."
            
        if user['is_approved'] != 1 and user['role'] != 'admin':
             return False, None, "Cuenta pendiente de aprobación por administrador."

        # ÉXITO
        return True, user['role'], "Login exitoso"
    finally:
        conn.close()
