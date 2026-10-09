"""Enriquecimiento de leads (Sprint 17): proveedores, cache, fusion y orquestador.

- `base`: el contrato de un proveedor (`EnrichmentProvider`) y sus errores.
- `registry`: registro de proveedores por nombre; el tenant elige cuales usar.
- `domain`: el dominio de la empresa a partir del lead (sin correos gratuitos).
- `cache`: cache por tenant de los datos de EMPRESA (los de la persona no se cachean).
- `merge`: combinar respuestas de varios proveedores y volcarlas al lead sin pisar lo escrito.
- `engine`: `enriquecer_lead()`, que junta todo lo anterior.

Las integraciones concretas (Apollo, Hunter, Clearbit...), la tarea de Celery y el endpoint
`POST /leads/{id}/enrich` son del slice de Dev B: se registran con `@register_provider` y
devuelven `CompanyData`/`PersonData` (`app/schemas/lead_scoring.py`).
"""
