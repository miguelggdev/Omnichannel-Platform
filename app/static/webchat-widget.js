/*!
 * Widget embebible del Webchat (Sprint 9). Un solo archivo, sin dependencias.
 *
 * Instalación en la página del cliente:
 *
 *   <script
 *     src="https://tu-api.example.com/api/v1/webchat/widget.js"
 *     data-token="EL_WEBCHAT_CHANNEL_TOKEN"
 *     data-title="Habla con nosotros"
 *     data-color="#2563eb"
 *   ></script>
 *
 * El token NO es un secreto (identifica el canal, no autentica: lo que
 * autentica al visitante es la sesión firmada que emite el servidor en el
 * primer frame — ver ADR-059/ADR-062 en MEMORY.md). El mismo archivo sirve
 * para cualquier tenant: todo lo que cambia lo ponen los atributos `data-*`
 * del propio `<script>`.
 *
 * Protocolo (app/schemas/webchat.py):
 *   cliente  -> {type:"hello", session, last_message_id, name}   (primer frame)
 *   cliente  -> {type:"message", message_id, text}
 *   cliente  -> {type:"button_reply", message_id, id, title}
 *   cliente  -> {type:"ping"}
 *   servidor -> {type:"connected", session}
 *   servidor -> {type:"message", message_id, text, timestamp, buttons?}
 *   servidor -> {type:"ack", message_id}
 *   servidor -> {type:"error", code, message}
 *   servidor -> {type:"pong"}
 *
 * Alcance deliberado: referencia funcional y accesible, no un design system.
 * Sin build step (se sirve tal cual desde el backend); sin adjuntos (el canal
 * tampoco los soporta todavía). Aislado del CSS de la página con Shadow DOM
 * cuando el navegador lo soporta.
 */
(function () {
  "use strict";

  // Capturado ANTES de cualquier operacion async: es la unica forma fiable de
  // saber cual es "este" <script> (document.currentScript deja de apuntar
  // aqui en cuanto el hilo cede el control).
  var scriptEl = document.currentScript;
  if (!scriptEl) {
    return; // cargado de una forma que no deja identificar el <script> (raro)
  }

  var config = leerConfig(scriptEl);
  if (!config.token) {
    console.error("[webchat] falta data-token en el <script>; el widget no arranca");
    return;
  }

  // ─── Configuracion ──────────────────────────────────────────────────────

  function leerConfig(el) {
    var ds = el.dataset || {};
    return {
      token: ds.token || "",
      wsUrl: ds.wsUrl || derivarWsUrl(el.src, ds.token || ""),
      title: ds.title || "Chat",
      color: ds.color || "#2563eb",
      position: ds.position === "left" ? "left" : "right",
      greeting: ds.greeting || "",
      visitorName: ds.visitorName || null,
      // Debe coincidir con WEBCHAT_MAX_MESSAGE_CHARS (default en Settings): el
      // servidor es quien manda de verdad, esto solo evita un envio inutil.
      maxChars: parseInt(ds.maxChars, 10) || 4000,
    };
  }

  function derivarWsUrl(scriptSrc, token) {
    try {
      var u = new URL(scriptSrc, window.location.href);
      var proto = u.protocol === "https:" ? "wss:" : "ws:";
      return proto + "//" + u.host + "/api/v1/webchat/" + encodeURIComponent(token);
    } catch (e) {
      return null;
    }
  }

  if (!config.wsUrl) {
    console.error("[webchat] no se pudo determinar la URL del WebSocket");
    return;
  }

  // ─── Estado persistido (por visitante, en ESTE navegador) ──────────────
  //
  // Solo lo minimo para reconectar: la sesion firmada y el ultimo mensaje
  // visto. El historial de la conversacion NO se guarda aqui — el servidor
  // lo repone al reconectar (app/services/webchat_history.py); guardarlo
  // tambien en localStorage duplicaria la fuente de verdad sin necesidad.

  var STORAGE_KEY = "omnichannel_webchat:" + config.token;

  function leerEstado() {
    try {
      var crudo = window.localStorage.getItem(STORAGE_KEY);
      return crudo ? JSON.parse(crudo) : {};
    } catch (e) {
      return {}; // privado/bloqueado: se sigue funcionando, solo sin persistir
    }
  }

  function guardarEstado() {
    try {
      window.localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ session: estado.session, lastMessageId: estado.lastMessageId })
      );
    } catch (e) {
      /* privado/bloqueado/lleno: no es fatal, solo no persiste */
    }
  }

  var guardado = leerEstado();
  var estado = {
    session: guardado.session || null,
    lastMessageId: guardado.lastMessageId || null,
    ws: null,
    abierto: false,
    reconectando: 0,
    pingTimer: null,
    reconnectTimer: null,
    deshabilitadoPermanente: false,
    vistosEnEstaSesion: {}, // message_id -> true, evita duplicar en vivo+repuesto
  };

  // ─── Utilidades ──────────────────────────────────────────────────────────

  function generarId() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    // Fallback para navegadores sin crypto.randomUUID: igual de valido para
    // el patron ^[A-Za-z0-9_-]{1,64}$ que exige el servidor.
    var alfabeto = "abcdefghijklmnopqrstuvwxyz0123456789";
    var id = "";
    for (var i = 0; i < 24; i++) {
      id += alfabeto[Math.floor(Math.random() * alfabeto.length)];
    }
    return id;
  }

  function crearNodo(etiqueta, claseCss, texto) {
    var nodo = document.createElement(etiqueta);
    if (claseCss) nodo.className = claseCss;
    // textContent, nunca innerHTML: el texto puede venir de un cliente o de
    // un LLM, y no debe interpretarse como HTML bajo ninguna circunstancia.
    if (texto !== undefined && texto !== null) nodo.textContent = texto;
    return nodo;
  }

  // ─── Interfaz ────────────────────────────────────────────────────────────

  var raiz = document.createElement("div");
  raiz.setAttribute("data-omnichannel-webchat", "");
  raiz.style.all = "initial"; // que el CSS de la pagina no toque ni este host
  var sombra = raiz.attachShadow ? raiz.attachShadow({ mode: "open" }) : raiz;

  var estilos = document.createElement("style");
  estilos.textContent = construirCss(config.color, config.position);
  sombra.appendChild(estilos);

  var burbuja = crearNodo("button", "ocw-burbuja");
  burbuja.type = "button";
  burbuja.setAttribute("aria-label", config.title);
  burbuja.appendChild(crearNodo("span", "ocw-burbuja-icono", "💬"));

  var panel = crearNodo("div", "ocw-panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", config.title);
  panel.hidden = true;

  var cabecera = crearNodo("div", "ocw-cabecera");
  var tituloEl = crearNodo("span", "ocw-titulo", config.title);
  var estadoConexionEl = crearNodo("span", "ocw-estado-conexion");
  estadoConexionEl.setAttribute("aria-hidden", "true");
  var cerrarBtn = crearNodo("button", "ocw-cerrar", "\u2715");
  cerrarBtn.type = "button";
  cerrarBtn.setAttribute("aria-label", "Cerrar el chat");
  cabecera.appendChild(tituloEl);
  cabecera.appendChild(estadoConexionEl);
  cabecera.appendChild(cerrarBtn);

  var mensajesEl = crearNodo("div", "ocw-mensajes");
  mensajesEl.setAttribute("role", "log");
  mensajesEl.setAttribute("aria-live", "polite");

  var avisoEl = crearNodo("div", "ocw-aviso");
  avisoEl.hidden = true;

  var formulario = document.createElement("form");
  formulario.className = "ocw-formulario";
  var input = document.createElement("textarea");
  input.className = "ocw-input";
  input.setAttribute("placeholder", "Escribe un mensaje...");
  input.setAttribute("maxlength", String(config.maxChars));
  input.setAttribute("rows", "1");
  input.setAttribute("aria-label", "Mensaje");
  var enviarBtn = crearNodo("button", "ocw-enviar", "Enviar");
  enviarBtn.type = "submit";
  formulario.appendChild(input);
  formulario.appendChild(enviarBtn);

  panel.appendChild(cabecera);
  panel.appendChild(mensajesEl);
  panel.appendChild(avisoEl);
  panel.appendChild(formulario);

  sombra.appendChild(panel);
  sombra.appendChild(burbuja);

  function montar() {
    if (document.body) {
      document.body.appendChild(raiz);
    } else {
      document.addEventListener("DOMContentLoaded", function () {
        document.body.appendChild(raiz);
      });
    }
  }

  function alternarPanel(abrir) {
    estado.abierto = abrir;
    panel.hidden = !abrir;
    burbuja.setAttribute("aria-expanded", abrir ? "true" : "false");
    if (abrir) {
      input.focus();
      mensajesEl.scrollTop = mensajesEl.scrollHeight;
    }
  }

  burbuja.addEventListener("click", function () {
    alternarPanel(!estado.abierto);
  });
  cerrarBtn.addEventListener("click", function () {
    alternarPanel(false);
  });

  input.addEventListener("keydown", function (evt) {
    // Enter envia, Shift+Enter hace un salto de linea (convencion habitual de chat).
    if (evt.key === "Enter" && !evt.shiftKey) {
      evt.preventDefault();
      formulario.requestSubmit ? formulario.requestSubmit() : enviarBtn.click();
    }
  });

  formulario.addEventListener("submit", function (evt) {
    evt.preventDefault();
    var texto = input.value.trim();
    if (!texto) return;
    input.value = "";
    enviarMensaje(texto);
  });

  if (config.greeting) {
    agregarBurbujaMensaje(config.greeting, "entrante", null);
  }

  // ─── Render de mensajes ──────────────────────────────────────────────────

  function agregarBurbujaMensaje(texto, direccion, messageId) {
    var fila = crearNodo("div", "ocw-fila ocw-fila-" + direccion);
    var burbujaMsg = crearNodo("div", "ocw-burbuja-msg", texto);
    if (messageId) fila.setAttribute("data-message-id", messageId);
    fila.appendChild(burbujaMsg);
    mensajesEl.appendChild(fila);
    mensajesEl.scrollTop = mensajesEl.scrollHeight;
    return fila;
  }

  function agregarBotones(botones) {
    if (!botones || !botones.length) return;
    var fila = crearNodo("div", "ocw-botones");
    botones.forEach(function (boton) {
      var btn = crearNodo("button", "ocw-boton", boton.title);
      btn.type = "button";
      btn.addEventListener("click", function () {
        fila.remove(); // un boton se usa una vez
        enviarRespuestaBoton(boton.id, boton.title);
      });
      fila.appendChild(btn);
    });
    mensajesEl.appendChild(fila);
    mensajesEl.scrollTop = mensajesEl.scrollHeight;
  }

  function mostrarAviso(texto) {
    avisoEl.textContent = texto;
    avisoEl.hidden = false;
    window.clearTimeout(mostrarAviso._t);
    mostrarAviso._t = window.setTimeout(function () {
      avisoEl.hidden = true;
    }, 4000);
  }

  function marcarEstadoConexion(estadoTexto) {
    estadoConexionEl.setAttribute("data-estado", estadoTexto);
    estadoConexionEl.title =
      estadoTexto === "online"
        ? "Conectado"
        : estadoTexto === "unavailable"
          ? "Chat no disponible"
          : "Conectando...";
  }

  // ─── WebSocket ───────────────────────────────────────────────────────────

  function enviarFrame(frame) {
    if (estado.ws && estado.ws.readyState === WebSocket.OPEN) {
      estado.ws.send(JSON.stringify(frame));
      return true;
    }
    return false;
  }

  function enviarMensaje(texto) {
    var messageId = generarId();
    var fila = agregarBurbujaMensaje(texto, "saliente", messageId);
    fila.classList.add("ocw-pendiente");
    if (!enviarFrame({ type: "message", message_id: messageId, text: texto })) {
      fila.classList.add("ocw-fallido");
      mostrarAviso("No se pudo enviar: reconectando...");
    }
  }

  function enviarRespuestaBoton(id, title) {
    var messageId = generarId();
    var fila = agregarBurbujaMensaje(title, "saliente", messageId);
    fila.classList.add("ocw-pendiente");
    if (!enviarFrame({ type: "button_reply", message_id: messageId, id: id, title: title })) {
      fila.classList.add("ocw-fallido");
    }
  }

  function alRecibirMensajeDelServidor(frame) {
    // El servidor puede reenviar en vivo algo que ya llego por la reposicion
    // al reconectar (o al reves); el message_id es el mismo en las dos vias.
    if (estado.vistosEnEstaSesion[frame.message_id]) return;
    estado.vistosEnEstaSesion[frame.message_id] = true;

    agregarBurbujaMensaje(frame.text || "", "entrante", frame.message_id);
    agregarBotones(frame.buttons);
    estado.lastMessageId = frame.message_id;
    guardarEstado();

    if (!estado.abierto) {
      burbuja.classList.add("ocw-burbuja-notificacion");
    }
  }

  function alRecibirAck(frame) {
    var fila = mensajesEl.querySelector('[data-message-id="' + cssEscape(frame.message_id) + '"]');
    if (fila) fila.classList.remove("ocw-pendiente", "ocw-fallido");
  }

  function alRecibirError(frame) {
    var TEXTOS = {
      rate_limited: "Estas escribiendo muy rapido, espera un momento.",
      message_too_long: "El mensaje es demasiado largo.",
      frame_too_large: "El mensaje es demasiado grande.",
      queue_unavailable: "No se pudo enviar, intenta de nuevo.",
      invalid_frame: "Hubo un problema enviando el mensaje.",
    };
    mostrarAviso(TEXTOS[frame.code] || frame.message || "Ocurrio un error.");
  }

  function cssEscape(valor) {
    // CSS.escape no esta en todos los navegadores antiguos; el patron real
    // de message_id ([A-Za-z0-9_-]) no necesita mas que esto.
    return String(valor).replace(/["\\]/g, "\\$&");
  }

  function conectar() {
    if (estado.deshabilitadoPermanente) return;
    marcarEstadoConexion("connecting");

    var ws;
    try {
      ws = new WebSocket(config.wsUrl);
    } catch (e) {
      programarReconexion();
      return;
    }
    estado.ws = ws;

    ws.addEventListener("open", function () {
      estado.reconectando = 0;
      enviarFrame({
        type: "hello",
        session: estado.session,
        last_message_id: estado.lastMessageId,
        name: config.visitorName,
      });
      iniciarPing();
    });

    ws.addEventListener("message", function (evt) {
      var frame;
      try {
        frame = JSON.parse(evt.data);
      } catch (e) {
        return; // frame corrupto: se ignora, no se rompe la conexion
      }
      switch (frame.type) {
        case "connected":
          estado.session = frame.session;
          guardarEstado();
          marcarEstadoConexion("online");
          break;
        case "message":
          alRecibirMensajeDelServidor(frame);
          break;
        case "ack":
          alRecibirAck(frame);
          break;
        case "error":
          alRecibirError(frame);
          break;
        case "pong":
          break;
      }
    });

    ws.addEventListener("close", function (evt) {
      detenerPing();
      marcarEstadoConexion("connecting");
      // 4401 (token invalido) y 4403 (origen no permitido): reintentar no
      // arregla nada, es un problema de configuracion de quien instalo el
      // widget. Cualquier otro codigo (idle, red, reinicio del servidor) se
      // reintenta con backoff.
      if (evt.code === 4401 || evt.code === 4403) {
        estado.deshabilitadoPermanente = true;
        marcarEstadoConexion("unavailable");
        return;
      }
      programarReconexion();
    });

    ws.addEventListener("error", function () {
      /* "close" se dispara despues igual: ahi se decide que hacer */
    });
  }

  function programarReconexion() {
    estado.reconectando += 1;
    var espera = Math.min(30000, 1000 * Math.pow(2, estado.reconectando - 1));
    espera += Math.random() * 500; // jitter: evita que todos los clientes reconecten a la vez
    window.clearTimeout(estado.reconnectTimer);
    estado.reconnectTimer = window.setTimeout(conectar, espera);
  }

  function iniciarPing() {
    detenerPing();
    // Bien por debajo de WEBCHAT_IDLE_TIMEOUT_SECONDS (120s por defecto) para
    // que la conexion nunca se de por inactiva mientras la pestana este abierta.
    estado.pingTimer = window.setInterval(function () {
      enviarFrame({ type: "ping" });
    }, 20000);
  }

  function detenerPing() {
    window.clearInterval(estado.pingTimer);
  }

  // Pausar el ping (y por tanto dejar que el servidor cierre por inactividad)
  // cuando la pestana esta oculta ahorra una conexion abierta por cada pestana
  // en segundo plano; al volver a mostrarse, se reconecta si hacia falta.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && !estado.deshabilitadoPermanente) {
      if (!estado.ws || estado.ws.readyState === WebSocket.CLOSED) {
        estado.reconectando = 0;
        conectar();
      }
    }
  });

  window.addEventListener("online", function () {
    if (!estado.deshabilitadoPermanente && (!estado.ws || estado.ws.readyState === WebSocket.CLOSED)) {
      estado.reconectando = 0;
      conectar();
    }
  });

  function construirCss(color, posicion) {
    var lado = posicion === "left" ? "left" : "right";
    return (
      ":host, div, button, textarea, form, span { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }\n" +
      ".ocw-burbuja { position: fixed; bottom: 20px; " +
      lado +
      ": 20px; width: 56px; height: 56px; border-radius: 50%; border: none; background: " +
      color +
      "; color: #fff; font-size: 24px; cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,.25); z-index: 2147483000; }\n" +
      ".ocw-burbuja:hover { filter: brightness(1.05); }\n" +
      ".ocw-burbuja-notificacion::after { content: ''; position: absolute; top: 4px; right: 4px; width: 10px; height: 10px; border-radius: 50%; background: #ef4444; }\n" +
      ".ocw-panel { position: fixed; bottom: 88px; " +
      lado +
      ": 20px; width: 340px; max-width: calc(100vw - 40px); height: 480px; max-height: calc(100vh - 120px); background: #fff; border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,.2); display: flex; flex-direction: column; overflow: hidden; z-index: 2147483000; }\n" +
      ".ocw-cabecera { background: " +
      color +
      "; color: #fff; padding: 14px 16px; display: flex; align-items: center; gap: 8px; }\n" +
      ".ocw-titulo { font-weight: 600; font-size: 15px; flex: 1; }\n" +
      ".ocw-estado-conexion { width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; }\n" +
      ".ocw-estado-conexion[data-estado='online'] { background: #22c55e; }\n" +
      ".ocw-estado-conexion[data-estado='unavailable'] { background: #ef4444; }\n" +
      ".ocw-cerrar { background: transparent; border: none; color: #fff; font-size: 16px; cursor: pointer; line-height: 1; padding: 4px; }\n" +
      ".ocw-mensajes { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; background: #f9fafb; }\n" +
      ".ocw-fila { display: flex; }\n" +
      ".ocw-fila-entrante { justify-content: flex-start; }\n" +
      ".ocw-fila-saliente { justify-content: flex-end; }\n" +
      ".ocw-burbuja-msg { max-width: 80%; padding: 8px 12px; border-radius: 14px; font-size: 14px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }\n" +
      ".ocw-fila-entrante .ocw-burbuja-msg { background: #fff; border: 1px solid #e5e7eb; color: #111827; border-bottom-left-radius: 4px; }\n" +
      ".ocw-fila-saliente .ocw-burbuja-msg { background: " +
      color +
      "; color: #fff; border-bottom-right-radius: 4px; }\n" +
      ".ocw-pendiente .ocw-burbuja-msg { opacity: .6; }\n" +
      ".ocw-fallido .ocw-burbuja-msg { outline: 2px solid #ef4444; }\n" +
      ".ocw-botones { display: flex; flex-wrap: wrap; gap: 6px; }\n" +
      ".ocw-boton { border: 1px solid " +
      color +
      "; color: " +
      color +
      "; background: #fff; border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer; }\n" +
      ".ocw-boton:hover { background: " +
      color +
      "; color: #fff; }\n" +
      ".ocw-aviso { padding: 6px 12px; font-size: 12px; color: #991b1b; background: #fee2e2; }\n" +
      ".ocw-formulario { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #e5e7eb; background: #fff; }\n" +
      ".ocw-input { flex: 1; resize: none; border: 1px solid #d1d5db; border-radius: 8px; padding: 8px 10px; font-size: 14px; max-height: 90px; }\n" +
      ".ocw-input:focus { outline: 2px solid " +
      color +
      "; outline-offset: 1px; }\n" +
      ".ocw-enviar { background: " +
      color +
      "; color: #fff; border: none; border-radius: 8px; padding: 0 14px; font-size: 14px; cursor: pointer; }\n" +
      ".ocw-enviar:hover { filter: brightness(1.05); }\n" +
      "@media (max-width: 480px) { .ocw-panel { right: 10px; left: 10px; width: auto; bottom: 80px; } }\n"
    );
  }

  montar();
  conectar();
})();
