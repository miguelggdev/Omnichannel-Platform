"""Payloads de ejemplo de webhooks de Meta (Instagram DM + Facebook Messenger).

Estructura real de la Graph API: `object` identifica el producto (`instagram` o
`page`), y `entry[].messaging[]` trae los eventos. El `sender.id` es un PSID
(Page-Scoped ID), no el ID real del usuario.

Todos los IDs son ficticios. No usar valores reales de una app de Meta aqui.
"""

from typing import Any

# ─── Instagram Direct ────────────────────────────────────────────────────────

INSTAGRAM_TEXT: dict[str, Any] = {
    "object": "instagram",
    "entry": [
        {
            "id": "17841400000000001",
            "time": 1757462400,
            "messaging": [
                {
                    "sender": {"id": "6789000000000001"},
                    "recipient": {"id": "17841400000000001"},
                    "timestamp": 1757462400000,
                    "message": {
                        "mid": "aWc6bXNnOjAwMDAwMDAx",
                        "text": "Hola, quiero informacion sobre los horarios",
                    },
                }
            ],
        }
    ],
}

INSTAGRAM_IMAGE: dict[str, Any] = {
    "object": "instagram",
    "entry": [
        {
            "id": "17841400000000001",
            "time": 1757462460,
            "messaging": [
                {
                    "sender": {"id": "6789000000000001"},
                    "recipient": {"id": "17841400000000001"},
                    "timestamp": 1757462460000,
                    "message": {
                        "mid": "aWc6bXNnOjAwMDAwMDAy",
                        "attachments": [
                            {
                                "type": "image",
                                "payload": {
                                    "url": "https://scontent.cdninstagram.com/v/ejemplo.jpg"
                                },
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

INSTAGRAM_QUICK_REPLY: dict[str, Any] = {
    "object": "instagram",
    "entry": [
        {
            "id": "17841400000000001",
            "time": 1757462520,
            "messaging": [
                {
                    "sender": {"id": "6789000000000001"},
                    "recipient": {"id": "17841400000000001"},
                    "timestamp": 1757462520000,
                    "message": {
                        "mid": "aWc6bXNnOjAwMDAwMDAz",
                        "text": "Si, agendar",
                        "quick_reply": {"payload": "AGENDAR_CITA"},
                    },
                }
            ],
        }
    ],
}

# ─── Facebook Messenger ──────────────────────────────────────────────────────

FACEBOOK_TEXT: dict[str, Any] = {
    "object": "page",
    "entry": [
        {
            "id": "1020000000000001",
            "time": 1757462580,
            "messaging": [
                {
                    "sender": {"id": "5432000000000001"},
                    "recipient": {"id": "1020000000000001"},
                    "timestamp": 1757462580000,
                    "message": {
                        "mid": "bWlkLjAwMDAwMDA0",
                        "text": "Buenas tardes, siguen abiertos?",
                    },
                }
            ],
        }
    ],
}

FACEBOOK_POSTBACK: dict[str, Any] = {
    "object": "page",
    "entry": [
        {
            "id": "1020000000000001",
            "time": 1757462640,
            "messaging": [
                {
                    "sender": {"id": "5432000000000001"},
                    "recipient": {"id": "1020000000000001"},
                    "timestamp": 1757462640000,
                    "postback": {
                        "mid": "bWlkLjAwMDAwMDA1",
                        "title": "Empezar",
                        "payload": "GET_STARTED",
                    },
                }
            ],
        }
    ],
}

# ─── YCloud (WhatsApp) ───────────────────────────────────────────────────────

YCLOUD_TEXT: dict[str, Any] = {
    "id": "evt_00000000000000000001",
    "type": "whatsapp.inbound_message.received",
    "createTime": "2026-09-09T20:00:00.000Z",
    "whatsappInboundMessage": {
        "id": "wamid.TEST0000000000000001",
        "wabaId": "100000000000001",
        "from": "573001112233",
        "to": "573009998877",
        "type": "text",
        "text": {"body": "Hola, necesito ayuda"},
        "timestamp": "2026-09-09T20:00:00.000Z",
    },
}

YCLOUD_IMAGE: dict[str, Any] = {
    "id": "evt_00000000000000000002",
    "type": "whatsapp.inbound_message.received",
    "createTime": "2026-09-09T20:01:00.000Z",
    "whatsappInboundMessage": {
        "id": "wamid.TEST0000000000000002",
        "wabaId": "100000000000001",
        "from": "573001112233",
        "to": "573009998877",
        "type": "image",
        "image": {
            "url": "https://media.ycloud.com/ejemplo.jpg",
            "caption": "Esta es la factura",
        },
        "timestamp": "2026-09-09T20:01:00.000Z",
    },
}
