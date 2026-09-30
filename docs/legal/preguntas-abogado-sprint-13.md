# Sprint 13 (agente clínico y canal de voz): preguntas para el abogado

> **Esto no es asesoría legal.** Es una lista de riesgos y preguntas fundamentada en
> la normativa colombiana, preparada por el equipo técnico para que un abogado
> habilitado la revise antes de operar con datos de salud reales. Donde una norma o
> una interpretación es dudosa se dice aquí mismo. Lo que dice `[PENDIENTE]` solo
> lo puede aportar el cliente o el abogado.

Fecha: 2026-09-30 · Alcance: `agente clinical`, `patient_consents`, `clinical_records`
(migraciones 016-018), transcripciones de llamadas (`call_records`, migración 019),
PIN de voz (migración 020) y el envío de texto clínico a OpenAI.

## Qué hace el software (para que el abogado sepa de qué hablamos)

| Aspecto | Cómo está hoy |
|---|---|
| Rol de la plataforma | SaaS que atiende **por cuenta de la clínica** (el tenant). La clínica decide para qué se usan los datos. |
| Quién dicta | Solo el profesional que el tenant declara (lista de contactos) y, por voz, solo si teclea un PIN en esa llamada. |
| Qué se guarda | Registro RIPS: documento del paciente, códigos CIE-10/CUPS y notas SOAP **cifrados** (pgcrypto); estado `draft → reviewed → signed → submitted`. |
| Humano en el circuito | El agente **propone** un borrador; un usuario de la clínica lo revisa y lo firma desde la API. El agente no firma ni envía. |
| Consentimiento del paciente | Se registra por paciente (`verbal`, `digital` o `written`), con fecha y quién lo registró. Sin él no se crea el registro. |
| Retención | Un trigger de la base impide borrar o modificar un registro firmado durante **20 años** desde la última atención del paciente. |
| Supresión (Ley 1581) | `anonymize` borra lo que la retención permite y conserva lo que ella obliga a guardar. |
| Terceros | OpenAI (LLM y Whisper/TTS en voz), Twilio (telefonía, y grabación si se activa), Supabase (base de datos). |
| Dictado fuera del historial | En `messages`, logs y checkpoints se guarda un marcador, no el texto. En la transcripción de una llamada clínica se redacta el texto de cada turno y se cifra la columna. |

---

## 🔴 Bloqueante (resolver antes de producción con pacientes reales)

### 1. ¿Cuántos años hay que conservar la historia clínica: 15 o 20?

**Qué pasa.** El pedido original decía «290 años»; se interpretó como **20** y se citó la
Resolución 839 de 2017. La revisión de esta lista apunta a que esa cita **no es
segura**: la lectura habitual es que la Res. 839/2017 fijó **15 años** (5 en archivo de
gestión y 10 en el central) desde la última atención, modificando los 20 que traía la
Res. 1995 de 1999. El código guarda **20**.

**Norma.** Ley 23 de 1981; Res. 1995 de 1999; Res. 839 de 2017; Ley 594 de 2000 (archivos).

**Riesgo.** Conservar de más no incumple el plazo mínimo, pero choca con el principio de
finalidad y temporalidad de la Ley 1581 (art. 4) y con lo que la política de privacidad
le prometa al paciente. Conservar de menos, en cambio, sí incumple. Cambiarlo es una
migración sencilla; lo que no se puede es que la política de privacidad diga un plazo
y el software aplique otro.

**Preguntas.**
- ¿Cuál es el plazo vigente y desde qué fecha exacta corre (última atención, alta, cierre de la historia)?
- ¿Es un mínimo o un máximo? ¿Hay que **eliminar** al vencer, o puede conservarse?
- ¿Hay plazos distintos por tipo de historia (víctimas de violaciones de DD. HH., menores, casos en proceso judicial)?
- Si el plazo es 15, ¿nos conviene dejar 20 por prudencia o hay que ajustarlo? `[PENDIENTE]`

**Solución técnica lista.** `RETENTION_YEARS` (`app/models/clinical_record.py`) y el trigger
`clinical_records_protect_function` (migración 017, valor congelado). Se cambia con una
migración nueva; no hace falta tocar el resto.

### 2. Transferencia de datos de salud a OpenAI (Estados Unidos)

**Qué pasa.** El texto que dicta el profesional —que puede traer nombre, documento y
diagnóstico del paciente— viaja a OpenAI para que el modelo lo interprete. En llamadas,
el audio va a Whisper y la respuesta pasa por TTS. Son **datos sensibles** (art. 5).

**Norma.** Ley 1581 de 2012, arts. 5, 6 y 26; Decreto 1074 de 2015 (transmisiones);
Circular Externa 005 de 2017 de la SIC (Estados Unidos figura entre los países con nivel adecuado).

**Riesgo.** Que Estados Unidos sea un país adecuado resuelve la *transferencia*, **no** la
base legal ni el contrato. Sin contrato de transmisión con la clínica y sin garantías
del proveedor (no entrenar con los datos, retención mínima) el riesgo es alto: sanción
de la SIC de hasta 2.000 SMLMV a la empresa y 300 SMLMV a los administradores.

**Preguntas.**
- ¿Qué modalidad de OpenAI está contratada (API estándar, retención cero, acuerdo empresarial)? **Verificarlo por escrito**: los términos por defecto pueden variar y cambian. `[PENDIENTE]`
- ¿Existe (o hay que firmar) un DPA con OpenAI que prohíba entrenar con los datos, fije la retención y obligue a notificar incidentes?
- ¿Basta la autorización del paciente al clínico para cubrir el envío a OpenAI, o el aviso de privacidad tiene que nombrarlo?
- Twilio: ¿la grabación de llamadas (`recording_url`) está activa? Si sí, ¿qué aviso hay que dar al inicio de la llamada y dónde se almacena?

**Solución.** Contrato de transmisión entre la plataforma (encargado) y cada clínica
(responsable); DPA con OpenAI y con Twilio; **minimizar antes de enviar** (ver
Recomendación A).

### 3. Contrato de transmisión y roles

**Qué pasa.** La plataforma actúa como **encargado** y la clínica como **responsable**.
Hoy no hay un texto contractual en el repositorio que lo diga.

**Norma.** Ley 1581, arts. 17-18; Decreto 1074 de 2015 (contrato de transmisión).

**Preguntas.**
- ¿Qué cláusulas mínimas debe llevar el contrato de transmisión (finalidad, deberes del encargado, subencargados —OpenAI, Twilio, Supabase—, auditoría, devolución o destrucción al terminar)?
- Al terminar el contrato con una clínica, ¿qué pasa con los registros firmados que la retención obliga a conservar: quién los custodia y dónde?
- ¿La plataforma debe inscribirse en el RNBD? (Depende de los activos: umbral de 100.000 UVT.) `[PENDIENTE]`

---

## 🟡 Importante

### 4. Autorización del paciente: ¿basta la «verbal» registrada por el profesional?

**Qué pasa.** El profesional dice al agente que el paciente autorizó y el sistema anota
`verbal`, fecha y quién lo registró. No guarda el **texto** ni la **versión** de lo que se le dijo al paciente.

**Norma.** Ley 1581, arts. 5, 9 y 12; Decreto 1074 de 2015 (prueba de la autorización).

**Riesgo.** Para datos sensibles la autorización debe ser previa, expresa e informada, y
hay que poder **probarla** (fecha, versión del texto). El silencio nunca vale.

**Preguntas.**
- ¿Una autorización verbal registrada así es prueba suficiente frente a la SIC?
- ¿Hay que informar al paciente que no está obligado a autorizar, y que el tratamiento incluye una herramienta de IA y proveedores en el exterior? ¿Con qué texto?
- Menores de edad: ¿autoriza el representante legal, y cómo se deja constancia? El sistema hoy no distingue.

**Solución técnica posible.** Guardar la versión del texto leído al paciente
(`consent_text_version`) y adjuntar la constancia. Se hace cuando el abogado defina el texto.

### 5. Supresión frente a retención

**Qué pasa.** Si un paciente pide borrar sus datos, el sistema conserva los registros
firmados mientras dure la retención y anonimiza el resto.

**Norma.** Ley 1581, art. 8 (supresión) y norma sectorial de historia clínica; la especial
prevalece sobre la general.

**Preguntas.**
- ¿La respuesta al paciente debe explicar el límite y citar el plazo? ¿Con qué texto?
- Al vencer la retención, ¿la eliminación es obligatoria o facultativa, y con qué procedimiento (acta de eliminación)?

### 6. PIN por DTMF: ¿alcanza como autenticación del profesional?

**Qué pasa.** El profesional teclea un PIN de 6-8 dígitos en la llamada. No es biométrico.
El caller ID por sí solo no autoriza, porque se falsifica.

**Preguntas.**
- ¿La normativa de historia clínica exige un mecanismo de autenticación concreto para quien la diligencia o la firma? (La firma del registro es aparte y la hace un usuario de la clínica desde la API, no por voz.)
- ¿Hay que llevar un registro de quién accedió a la historia clínica y cuándo, y por cuánto tiempo se conserva? (Hoy `audit_logs` guarda el hecho sin el contenido.)

### 7. Transcripciones de llamadas

**Qué pasa.** La transcripción de una llamada se guarda cifrada. Si la conversación fue
clínica, el texto de cada turno se reemplaza por `[contenido clínico protegido]`.

**Preguntas.**
- La transcripción de una llamada no clínica de un paciente, ¿es dato sensible aunque no se hable de salud?
- ¿Cuánto tiempo puede conservarse? Hoy no caduca. `[PENDIENTE]`

### 8. Responsabilidad por la IA

**Qué pasa.** No hay ley de IA vigente en Colombia (solo el CONPES 4144 de 2025 y un
proyecto de ley en trámite), pero si la IA trata datos personales aplica la Ley 1581 completa,
y si causa daño, la Ley 1480 y el Código Civil.

**Lo que está bien.** El agente propone y **un humano firma**: el registro no avanza de
`draft` sin revisión, la firma la hace un usuario autenticado de la clínica, y un
registro firmado no se modifica.

**Preguntas.**
- ¿Los términos de servicio deben decir que la salida del agente es una **sugerencia** que el profesional debe verificar? ¿Con qué texto en la interfaz?
- ¿Cómo se reparte la responsabilidad entre la plataforma y la clínica por un error de codificación (CIE-10, CUPS) que el profesional firma sin revisar?
- El agente no propone diagnósticos ni códigos inventados: solo usa los del catálogo. ¿Conviene decirlo en los términos?

---

## 🔵 Recomendaciones técnicas (no requieren abogado, pero mejoran la posición legal)

**A. Minimizar antes de enviar a OpenAI.** Hoy el modelo ve el nombre y el documento
que el profesional dicta. Reemplazarlos por marcadores antes de la llamada al modelo
y restituirlos al guardar reduce lo que sale del país. Es trabajo de ingeniería (no
hecho); conviene decidirlo con la respuesta a la pregunta 2.

**B. Caducidad y rotación del PIN.** Hoy no caduca y solo un admin puede cambiarlo.

**C. Texto del aviso al inicio de la llamada.** Si hay grabación o IA, informarlo en el saludo.

**D. Píxeles y analytics.** No poner píxeles publicitarios dentro del área autenticada
de una app de salud: enviar a un tercero qué página de tratamiento vio alguien es un
dato de salud sin base legal. (Hoy no hay ninguno; que no se agregue.)

---

## Lo que ya está resuelto en el software

- Datos sensibles cifrados en la base y consentimiento exigido **en código**, no en el prompt.
- Retención garantizada por la base de datos, no solo por la API.
- Supresión que respeta la retención.
- Sin contenido clínico en logs, checkpoints ni en `messages`.
- Aislamiento por tenant (RLS con FORCE) probado contra PostgreSQL real.
- Un humano revisa y firma; el agente no.

## Pendiente de abogado (lo que exige criterio profesional)

1. Plazo de retención vigente y su punto de partida (pregunta 1).
2. Suficiencia de la autorización verbal y su texto (pregunta 4).
3. Contrato de transmisión, DPA con OpenAI y Twilio, y aviso de privacidad (preguntas 2 y 3).
4. Inscripción en el RNBD.
5. Términos de servicio: naturaleza de sugerencia de la IA y reparto de responsabilidad.

## Anexo: datos que debe aportar el cliente antes de publicar

- `[PENDIENTE]` Modalidad contratada con OpenAI y con Twilio (por escrito).
- `[PENDIENTE]` Si la grabación de llamadas está activa, y dónde queda almacenada.
- `[PENDIENTE]` Activos de la empresa (para el umbral del RNBD).
- `[PENDIENTE]` Nombre del responsable de datos de cada clínica y canal de reclamos (consulta 10 días hábiles, reclamo 15).
