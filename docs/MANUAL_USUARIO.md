# Manual de usuario — Plataforma Omnicanal

Esta guía explica cómo usar el panel web de la plataforma: atender conversaciones de todos tus canales, gestionar contactos, entrenar al asistente con tus documentos y administrar tu equipo.

---

## Índice

1. [Conceptos básicos](#1-conceptos-básicos)
2. [Acceso a la plataforma](#2-acceso-a-la-plataforma)
3. [La interfaz](#3-la-interfaz)
4. [Roles y permisos](#4-roles-y-permisos)
5. [Inicio (panel de actividad)](#5-inicio-panel-de-actividad)
6. [Conversaciones](#6-conversaciones)
7. [Contactos](#7-contactos)
8. [Base de conocimiento](#8-base-de-conocimiento)
9. [Respuestas rápidas](#9-respuestas-rápidas)
10. [Perfil del negocio](#10-perfil-del-negocio)
11. [Equipo](#11-equipo)
12. [Sandbox y funciones](#12-sandbox-y-funciones)
13. [Preferencias](#13-preferencias)
14. [Plataforma (solo superadministrador)](#14-plataforma-solo-superadministrador)
15. [Preguntas frecuentes y solución de problemas](#15-preguntas-frecuentes-y-solución-de-problemas)

---

## 1. Conceptos básicos

| Término | Qué significa |
|---|---|
| **Cuenta / tenant** | Tu negocio dentro de la plataforma. Todo lo que ves (contactos, conversaciones, documentos) pertenece solo a tu cuenta; ningún otro negocio puede verlo. |
| **Canal** | Vía por la que te escriben tus clientes: WhatsApp, Instagram, Facebook, Telegram, correo, chat web o voz. |
| **Asistente (bot)** | La IA que responde automáticamente a tus clientes usando la información de tu base de conocimiento. |
| **Escalar / pasar a una persona** | Cuando el asistente no puede resolver algo, deja la conversación esperando a que una persona del equipo la atienda. |
| **Contacto** | Una persona que se comunica con tu negocio. Si escribe por varios canales, se agrupa en un único contacto. |

---

## 2. Acceso a la plataforma

### 2.1 Iniciar sesión

1. Abre la dirección del panel en tu navegador.
2. Escribe tu **Correo electrónico** y tu **Contraseña** (mínimo 8 caracteres).
3. Pulsa **Iniciar sesión**.

Si ves *«Credenciales inválidas»*, revisa el correo y la contraseña. Si ves *«Usuario desactivado»* u *«Organización suspendida»*, contacta con el administrador de tu cuenta o con el operador de la plataforma.

### 2.2 Crear una cuenta para tu negocio

Si el registro público está habilitado, en la pantalla de acceso aparece el enlace **«¿Aún no tienes cuenta? Crea la de tu negocio»**. El asistente de registro tiene tres pasos:

1. **Negocio:** nombre del negocio (mínimo 2 caracteres), tipo (Restaurante, Clínica, Tienda online, Servicios, Educación u Otro) y país.
2. **Tu cuenta:** nombre completo, correo y contraseña. La contraseña debe tener entre 8 y 72 caracteres e incluir **mayúscula, minúscula y número**.
3. **Confirmar:** revisa los datos, acepta los términos y condiciones y pulsa **Crear cuenta**.

Quien crea la cuenta queda como **Administrador**.

### 2.3 Verificar tu correo

Tras registrarte recibirás un enlace de verificación. Mientras no lo uses, verás un aviso en la parte superior del panel con la opción **Reenviar enlace**. Si el enlace caducó, inicia sesión y pide uno nuevo desde ese aviso.

> Si no hay enlace de registro en la pantalla de acceso, el registro público está desactivado: pide a un administrador que te cree un usuario (ver [Equipo](#11-equipo)).

---

## 3. La interfaz

- **Menú lateral (izquierda):** acceso a todas las secciones. Se puede contraer o expandir; en móvil se abre con el botón de menú. Solo muestra las secciones que tu rol puede usar.
- **Ruta de navegación (arriba):** indica dónde estás, por ejemplo *Ajustes › Equipo*. Puedes pulsar cualquier nivel para volver.
- **Idioma:** el selector con el código del idioma (ES, EN…) cambia la interfaz entre español, inglés, portugués, italiano, alemán y francés.
- **Tema:** el botón de pantalla alterna entre **Claro**, **Oscuro** o **Sistema** (el de tu dispositivo).
- **Menú de usuario:** muestra tu nombre y rol, y la opción **Cerrar sesión**.

---

## 4. Roles y permisos

Cada usuario tiene un rol. Los roles superiores pueden hacer todo lo que hacen los inferiores.

| Rol | Pensado para | Puede |
|---|---|---|
| **Superadministrador** | Operador de la plataforma | Todo, incluida la sección **Plataforma** (clientes, workers, sistema y seguridad). |
| **Administrador** | Dueño o responsable del negocio | Todo dentro de su cuenta: perfil del negocio, equipo, sandbox, borrar y reprocesar documentos, borrar respuestas rápidas, fusionar contactos. |
| **Supervisor** | Coordinador del equipo de atención | Atender conversaciones, **asignar conversaciones**, subir documentos, gestionar contactos y respuestas rápidas. |
| **Agente** | Persona que atiende a los clientes | Ver y responder conversaciones, cambiar su estado, gestionar contactos y notas, crear y editar respuestas rápidas, consultar la base de conocimiento. |
| **Profesional médico** | Personal clínico (cuentas de clínicas) | Acceso a la información clínica; no atiende conversaciones desde el panel. |

Si entras en una sección para la que tu rol no tiene permiso verás **«Sin acceso — Tu rol no tiene permiso para ver esta sección»**.

---

## 5. Inicio (panel de actividad)

La primera pantalla tras iniciar sesión resume la actividad de tu cuenta:

| Indicador | Qué muestra |
|---|---|
| **Esperan a una persona** | Conversaciones que el bot escaló y nadie ha tomado todavía. **Revísalo primero.** |
| **Conversaciones abiertas** | Conversaciones no resueltas, con las nuevas de los últimos 7 días. |
| **Contactos** | Total de contactos de tu cuenta. |
| **Documentos listos** | Documentos de la base de conocimiento que el asistente ya puede usar. |
| **Mensajes (24 h)** | Recibidos y enviados, comparados con las 24 h anteriores. |
| **Primera respuesta (mediana)** | Cuánto se tarda en dar la primera respuesta (del bot o de una persona) en los últimos 7 días. |
| **Satisfacción (CSAT)** | Valoración media de tus clientes en los últimos 30 días. |
| **Tokens del mes** | Consumo del presupuesto de IA de tu cuenta. |

También incluye dos gráficas (**Mensajes por día** de los últimos 7 días y **Conversaciones por canal** de los últimos 30) y accesos directos a Conversaciones, Base de conocimiento y Preferencias.

---

## 6. Conversaciones

### 6.1 Lista de conversaciones

Muestra las conversaciones de todos tus canales, ordenadas por la última actividad. Puedes filtrarlas por **estado** y por **canal**. Pulsa una conversación para abrirla.

### 6.2 Estados de una conversación

| Estado | Significado |
|---|---|
| **Nueva** | Acaba de llegar y nadie la ha atendido. |
| **Atiende el bot** | El asistente está respondiendo. |
| **Espera a una persona** | El bot la escaló: necesita que alguien del equipo la tome. |
| **Atiende una persona** | Un miembro del equipo la está atendiendo; el bot no contesta. |
| **Espera al cliente** | Se respondió y se espera la contestación del cliente. |
| **Resuelta** | El caso está cerrado. |
| **Archivada** | Guardada como histórico; ya no admite cambios. |

Solo se permiten estos cambios de estado (el botón **Pasar a: …** muestra únicamente los válidos):

```
Nueva ─────────────► Atiende el bot / Atiende una persona
Atiende el bot ────► Atiende una persona / Espera a una persona / Resuelta
Espera a una persona ► Atiende una persona / Resuelta
Atiende una persona ► Espera al cliente / Resuelta
Espera al cliente ─► Atiende una persona / Atiende el bot / Resuelta
Resuelta ──────────► Archivada
```

### 6.3 Atender una conversación

1. Abre la conversación. Verás el historial de mensajes identificados como **Contacto**, **Asistente**, **Agente** o **Sistema**. Si hay archivos, pulsa **Ver adjunto**.
2. Si eres supervisor o administrador, puedes pulsar **Asignármela** para quedártela.
3. Escribe en el cuadro **«Escribe una respuesta…»**:
   - **Enter** envía el mensaje.
   - **Mayús + Enter** inserta un salto de línea.
   - **/** abre el buscador de [respuestas rápidas](#9-respuestas-rápidas).
4. Cuando termines, cambia el estado con **Pasar a: Resuelta**.

> **Importante:** al responder, **tomas la conversación y el bot deja de contestar** en ella. Si quieres que el asistente vuelva a encargarse, cambia el estado a **Atiende el bot** (posible desde *Espera al cliente*).

Mensajes que puedes ver en el cuadro de respuesta:

- *«Esta conversación está cerrada y ya no admite respuestas»*: está Resuelta o Archivada.
- *«Esta conversación la atiende otra persona del equipo»*: está asignada a otro usuario.
- *«Revisa el texto: faltan datos para …»*: una respuesta rápida tiene variables que no se pudieron rellenar; complétalas a mano antes de enviar.

---

## 7. Contactos

### 7.1 Lista y búsqueda

Los contactos **se crean solos** cuando alguien escribe por cualquier canal. Usa el buscador para encontrarlos por nombre. Para añadir uno a mano pulsa **Nuevo contacto** e indica nombre, apellido y nombre a mostrar.

### 7.2 Ficha del contacto

Al abrir un contacto verás:

- **Canales y etiquetas:** los canales por los que se ha comunicado (número de WhatsApp, cuenta de Instagram, etc.).
- **Conversaciones:** su historial, con acceso directo a cada una.
- **Notas internas:** comentarios para tu equipo que **el cliente nunca ve**. Escribe la nota y pulsa **Añadir nota**.
- **Editar:** cambia sus datos y pulsa **Guardar**.

---

## 8. Base de conocimiento

Son los documentos con los que el asistente responde a tus clientes: catálogos, precios, preguntas frecuentes, políticas, horarios…

### 8.1 Subir documentos (supervisor o superior)

1. Arrastra los archivos al recuadro o haz clic para elegirlos.
2. Formatos admitidos: **PDF, DOCX, TXT, CSV, XLSX e imágenes**, hasta **50 MB** por archivo.
3. El documento pasa por estos estados:

| Estado | Significado |
|---|---|
| **En cola** | Esperando a ser procesado. |
| **Procesando** | Se está leyendo y dividiendo en fragmentos. |
| **Listo** | El asistente ya lo usa para responder. |
| **Con error** | No se pudo procesar. Revisa el archivo y usa **Reprocesar**. |

La tabla muestra título, estado, tamaño, número de **fragmentos** (partes en que se dividió el texto) y fecha de subida.

### 8.2 Reprocesar o eliminar (administrador)

- **Reprocesar:** vuelve a procesar el documento (útil si falló).
- **Eliminar:** el documento y sus fragmentos dejan de usarse en las respuestas. **No se puede deshacer.**

> **Consejo:** documentos claros y bien estructurados (títulos, listas, preguntas y respuestas) producen mejores respuestas del asistente.

---

## 9. Respuestas rápidas

Mensajes predefinidos que tu equipo envía con un atajo.

### 9.1 Crear una respuesta

Pulsa **Nueva respuesta** y completa:

| Campo | Indicaciones |
|---|---|
| **Atajo** | Empieza por `/` y usa solo minúsculas, números, guion y guion bajo. Ejemplo: `/saludo`. |
| **Título** | Nombre descriptivo. |
| **Mensaje** | El texto que se enviará. |
| **Categoría** | Opcional, para organizarlas. |

### 9.2 Variables

Dentro del mensaje puedes usar variables que se rellenan al enviar:

| Variable | Se sustituye por |
|---|---|
| `{{contact_name}}` | Nombre del contacto. |
| `{{agent_name}}` | Tu nombre. |
| `{{ticket_id}}` | Identificador de la conversación. |
| `{{date}}` | Fecha actual. |

Ejemplo: `Hola {{contact_name}}, soy {{agent_name}}. ¿En qué puedo ayudarte?`

### 9.3 Usarlas en una conversación

En el cuadro de respuesta escribe **/** y elige la respuesta de la lista. Puedes modificar el texto antes de enviarlo.

Cualquier agente puede crear y editar respuestas rápidas; **solo los administradores pueden eliminarlas**.

---

## 10. Perfil del negocio

*Solo administradores.* Reúne los datos de tu negocio en seis secciones:

| Sección | Contenido |
|---|---|
| **Negocio** | Nombre, tipo y descripción. |
| **Contacto y ubicación** | Teléfono, correo, sitio web, dirección, ciudad, país (código de dos letras, p. ej. `CO`) y zona horaria (p. ej. `America/Bogota`). |
| **Horario de atención** | Apertura y cierre por día, en hora local y formato 24 h. Marca **Cerrado** los días sin atención. |
| **Marca** | Colores principal y secundario (formato `#1D4ED8`) y logotipo. El logo puede subirse (PNG, JPEG o WebP, hasta 512 KB) o indicarse con un enlace `https`. |
| **Redes sociales** | Usuario de Instagram y X/Twitter, enlace de Facebook y número de WhatsApp. |
| **Asistente** | **Mensaje de bienvenida** (al abrir una conversación) y **Mensaje al pasar con una persona** (al escalar). |

Pulsa **Guardar** para aplicar los cambios o **Descartar cambios** para volver a lo guardado.

> **Nota:** hoy el asistente solo usa los dos mensajes de la sección **Asistente**. El resto de datos se guarda para funciones futuras (mensaje fuera de horario, widget, marca del panel). Si tu cuenta aún no tiene un asistente activo, esos dos mensajes no se pueden guardar.

---

## 11. Equipo

*Solo administradores.* Gestiona quién usa la plataforma en tu cuenta.

- **Buscar y filtrar:** por nombre o correo, por rol y por estado (Activo / Desactivado). La tabla muestra el último acceso de cada usuario.
- **Nuevo usuario:** indica nombre, apellido, correo, rol y una **contraseña inicial** (8 a 72 caracteres). Comunícasela a la persona por un canal seguro.
- **Editar:** cambia datos o rol. El campo **Nueva contraseña** es opcional: déjalo vacío para no cambiarla.
- **Desactivar / Reactivar:** un usuario desactivado ya no puede iniciar sesión. Si tenía la sesión abierta, seguirá funcionando unos minutos hasta que caduque.

> No puedes cambiar tu propio rol: pídeselo a otra persona administradora.

---

## 12. Sandbox y funciones

*Solo administradores.* El sandbox es una **copia de la configuración del asistente** para probar cambios sin afectar a tus clientes.

> Si ves *«El sandbox no está activado»*, pide al operador de la plataforma que active la función `enable_sandbox` para tu cuenta.

### 12.1 Flujo de trabajo

1. **Crear sandbox:** copia tu configuración de producción.
2. **Probar el asistente:** escribe mensajes de prueba en el chat de la sección. No se envían a nadie, pero **consumen tokens** de tu cuota de pruebas. Si el asistente decide escalar, verás la marca *«escaló a una persona»*.
3. **Publicar a producción:** la configuración del sandbox pasa a producción y la verán tus clientes. La versión anterior se guarda.
4. **Volver a la versión anterior:** si algo salió mal tras publicar, producción recupera la última configuración guardada.
5. **Rehacer desde producción:** descarta los cambios y las conversaciones de prueba del sandbox y vuelve a copiar producción.

### 12.2 Funciones de la cuenta

En la misma pantalla puedes activar o desactivar funciones de tu cuenta. Las marcadas como **Solo superadministrador** las gestiona el operador de la plataforma, y las marcadas como **Aún sin efecto** todavía no cambian el comportamiento.

---

## 13. Preferencias

Disponible para todos los usuarios. Elige el **Idioma** de la interfaz y el **Tema** (Claro, Oscuro o Sistema). Se guardan en tu cuenta y se aplican en cualquier dispositivo en el que inicies sesión.

---

## 14. Plataforma (solo superadministrador)

Sección para el operador que administra la plataforma completa.

| Pantalla | Para qué sirve |
|---|---|
| **Clientes** | Lista de negocios con estado, plan, usuarios, conversaciones y mensajes de los últimos 30 días. En el detalle de cada cliente: uso (contactos, documentos, tokens, asistente activo) y las acciones **Suspender** (con motivo opcional) o **Reactivar**. Por privacidad, **nunca se muestra el contenido de las conversaciones** de un cliente. No puedes suspender tu propia cuenta. |
| **Celery y Redis** | Workers conectados, mensajes pendientes en cada cola, estado de Redis y tareas en curso. Permite **revocar** tareas que aún no han empezado. Se actualiza cada 30 segundos. |
| **Sistema** | Estado de la base de datos, Redis y los workers (**Funciona** / **Con problemas**), con latencia, versión y entorno. |
| **Seguridad** | Comprobaciones de configuración (entorno, duración del token, CORS, secretos de webhooks, registro público, correo de verificación) y del aislamiento entre clientes (Row Level Security por tabla). Cada una aparece como **Correcto**, **Revisar** o **Corregir**, con una explicación. |

---

## 15. Preguntas frecuentes y solución de problemas

**El bot no responde en una conversación.**
Comprueba el estado. Si está en *Atiende una persona*, el bot no contesta: alguien respondió y la tomó. Pásala a *Atiende el bot* si quieres que vuelva a encargarse.

**El asistente responde con información incorrecta o incompleta.**
Revisa la [Base de conocimiento](#8-base-de-conocimiento): que el documento correcto esté en estado **Listo** y que su contenido esté actualizado. Si cambias un documento, elimina el antiguo y sube el nuevo.

**Un documento se queda en «En cola» o «Procesando» mucho tiempo.**
El procesamiento ocurre en segundo plano. Si no avanza, avisa al operador de la plataforma: puede que los workers no estén activos (se ve en *Plataforma › Celery y Redis*).

**No veo una sección del menú.**
Tu rol no tiene acceso. Consulta la tabla de [Roles y permisos](#4-roles-y-permisos) y pide a un administrador que revise tu rol.

**No puedo responder: «Esta conversación la atiende otra persona del equipo».**
Está asignada a otro usuario. Un supervisor o administrador puede reasignarla.

**Olvidé mi contraseña.**
Pide a un administrador de tu cuenta que te asigne una nueva desde [Equipo](#11-equipo), en el campo *Nueva contraseña*.

**«Algo salió mal» o «No se pudieron cargar los datos».**
Pulsa **Reintentar**. Si persiste, recarga la página. Si sigue fallando, avisa al operador de la plataforma.
