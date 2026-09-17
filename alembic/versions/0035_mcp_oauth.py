"""0035_mcp_oauth: acceso delegado al MCP (spec 002 T003, data-model.md
§Esquema) -- `oauth_clients`, `oauth_authorization_requests`, `oauth_grants`
y `oauth_tokens`.

Las cuatro tablas son NUEVAS y no se toca ninguna existente: no hay
expand/contract que planificar (data-model.md "Sin plan expand/contract"),
`owners` solo se referencia. Estilo de 0034: SQL literal via `op.execute`,
CHECKs explicitos, sin ORM.

SOLO HASHES, NUNCA CREDENCIALES EN CLARO (threat-model.md C-44). Las cuatro
columnas de secreto -- `oauth_clients.client_secret_hash`,
`oauth_authorization_requests.code_hash`, `oauth_tokens.token_hash` y el
enlace `oauth_tokens.rotated_from` -- exigen sha256 en hexadecimal por CHECK
(`^[a-f0-9]{64}$`) y llevan indice UNIQUE. Un `code`, un access o un refresh
en claro (base64url, 43 chars) no cabe en ninguna columna de este esquema:
el CHECK lo rechaza. `code_verifier` no tiene columna a proposito -- llega
en el canje y se compara contra `code_challenge`, no se guarda.

Invariantes que aplica la BD, no solo la aplicacion:

* Cliente publico sin secreto y cliente confidencial con secreto:
  `(token_endpoint_auth_method = 'none') = (client_secret_hash IS NULL)`.
* Un solo uso del codigo (C-38): `code_hash` es UNIQUE y el codigo vivo solo
  existe en estado CONSENTED --
  `(state = 'CONSENTED') = (code_hash IS NOT NULL AND redeemed_at IS NULL)`.
  Consecuencia operativa: caducar una solicitud CONSENTED es
  `SET state = 'EXPIRED', code_hash = NULL` (el hash del codigo muere con
  el codigo); una REDEEMED conserva su hash para detectar el replay.
* El `owner_id` solo se fija al consentir:
  `(consented_at IS NULL) = (owner_id IS NULL)`, y CONSENTED/REDEEMED exigen
  `consented_at`.
* Rotacion de refresh (C-43): como mucho un token ACTIVE por
  `(grant_id, kind)` -- indice UNIQUE parcial -- y como mucho un sucesor por
  token rotado -- `rotated_from` UNIQUE. Dos refrescos concurrentes con el
  mismo token no pueden emitir dos familias: uno de los dos choca.
* TTL (C-45): `expires_at > created_at` / `> issued_at`; la caducidad se
  comprueba tambien en cada verificacion, pero una fila sin ventana valida
  no entra.
* Vocabulario de alcances cerrado (contracts/oauth.md §3): cada scope
  almacenado pertenece a `{ads:read, ads:propose}`. Ampliarlo es una
  migracion, que es exactamente la friccion que se quiere.

ON DELETE (elegido uno a uno, ninguno por defecto):

* `oauth_authorization_requests.client_id -> oauth_clients` CASCADE: una
  solicitud en vuelo de un cliente podado (C-42) no es canjeable por nadie.
* `oauth_authorization_requests.owner_id -> owners` CASCADE: obligado por
  el CHECK `(consented_at IS NULL) = (owner_id IS NULL)` -- SET NULL dejaria
  la fila en un estado que la propia tabla prohibe. Ademas hace viable el
  borrado del propietario (derecho de supresion).
* `oauth_grants.owner_id -> owners` CASCADE: la concesion ES autoridad
  delegada de ese propietario; sin el no significa nada. RESTRICT bloquearia
  el borrado del propietario por una credencial de maquina.
* `oauth_grants.client_id -> oauth_clients` CASCADE: mismo motivo por el
  otro lado. Solo dispara en un borrado deliberado: la poda automatica (C-42)
  no toca clientes con concesion, y revocar desde el panel (C-55) marca
  `revoked_at`, no borra. La huella de auditoria vive en `decision_log`, no
  aqui.
* `oauth_grants.authorization_txn_id -> oauth_authorization_requests`
  SET NULL: la retencion borra solicitudes terminales a los 7 dias y la
  concesion tiene que sobrevivir a esa poda; lo unico que se pierde es el
  enlace al canje que la creo.
* `oauth_tokens.grant_id -> oauth_grants` CASCADE: los tokens son parte del
  agregado Grant, no pueden sobrevivirle.
* `oauth_tokens.rotated_from -> oauth_tokens` SET NULL: al podar tokens
  caducados de mas de 30 dias, el sucesor vivo debe quedarse (CASCADE
  borraria la cadena entera hacia adelante, RESTRICT bloquearia la poda).
  La deteccion de reuso no depende del enlace sino del estado ROTATED.

Indices (cada uno con la consulta que lo justifica):

* `ix_oauth_clients_created_at` -- poda C-42: `... WHERE created_at < now() -
  interval '24 hours' AND NOT EXISTS (concesion)`.
* `ix_oauth_clients_secret_hash` (UNIQUE) -- integridad: un secreto no puede
  pertenecer a dos clientes.
* `ix_oauth_authorization_requests_code_hash` (UNIQUE) -- el canje:
  `UPDATE ... WHERE code_hash = $1 AND state = 'CONSENTED' RETURNING ...`.
* `ix_oauth_authorization_requests_live` (parcial) -- barrido de caducidad
  C-58: `... WHERE state IN ('PENDING','CONSENTED') AND expires_at <= now()`.
* `ix_oauth_authorization_requests_client` -- tope de 5 txn pendientes por
  cliente (C-58) y la FK a `oauth_clients` (Postgres no la indexa solo).
* `ix_oauth_authorization_requests_owner` (parcial, la columna es nula en la
  mayoria de filas) -- la FK a `owners` en el borrado del propietario.
* `ix_oauth_grants_client` -- guarda de la poda C-42 (`EXISTS` sobre TODAS
  las concesiones del cliente, tambien revocadas) y la FK.
* `ix_oauth_grants_owner` -- "Agentes conectados" del panel (C-55):
  `... WHERE owner_id = $1 ORDER BY created_at DESC`, y la FK a `owners`.
* `ix_oauth_grants_authorization_txn` (UNIQUE) -- C-38: dado un codigo
  reutilizado, la concesion que nacio de el, para revocar la familia.
* `ix_oauth_tokens_grant_active` (UNIQUE parcial) -- el invariante de un
  token ACTIVE por clase y la lectura "el refresh vivo de esta concesion".
* `ix_oauth_tokens_expiry` (parcial) -- barrido de caducados C-58.
* `ix_oauth_tokens_rotated_from` (UNIQUE parcial) -- cadena de rotacion
  lineal y la auto-FK.
* `ix_oauth_tokens_grant` -- la FK a `oauth_grants` (el CASCADE recorre
  tokens de cualquier estado, no solo ACTIVE) y el detalle de una concesion.

Cambios respecto al DDL propuesto en data-model.md, todos deliberados:

1. `oauth_tokens.kind` en minuscula (`access`/`refresh`) en vez de
   `ACCESS`/`REFRESH`: en este repo los discriminadores van en minuscula
   (`platform_accounts.platform`, `approvals.kind`) y las maquinas de estado
   en mayuscula (`state`, `status`). `kind` es un discriminador.
2. `client_name` limitado a 100 chars (data-model.md decia 200) para que la
   BD aplique el mismo tope que C-42, no el doble.
3. Anadida `oauth_grants.authorization_txn_id` (UNIQUE, SET NULL): sin ella
   C-38 no puede ir del codigo reutilizado a la familia que revocar mas que
   adivinando por (owner, cliente, fecha).
4. `ix_oauth_grants_client` es total, no parcial `WHERE revoked_at IS NULL`:
   la guarda de poda pregunta por concesiones de cualquier estado y la FK
   necesita cubrir todas las filas.
5. El CHECK del codigo es la bicondicional pura; data-model.md la escribia
   con un `OR state <> 'CONSENTED'` final que la vuelve vacia para los demas
   estados (dejaba pasar un EXPIRED conservando su `code_hash` canjeable).
6. `ix_oauth_tokens_grant_active` es UNIQUE (en data-model.md no lo era): es
   el invariante "un access y un refresh ACTIVE por concesion" escrito donde
   la concurrencia no puede saltarselo.
7. Anadidos CHECKs de forma que data-model.md no detallaba: `code_challenge`
   43 chars base64url (C-36), `redirect_uri` https o bucle local (C-37),
   `resource` https sin query ni fragmento (C-46, RFC 8707), scopes dentro
   del vocabulario, y las bicondicionales de `redeemed_at`/`revoked_reason`.

Sin `updated_at`: estas filas no se "editan", transitan por estados con
marca de tiempo propia (`consented_at`, `redeemed_at`, `revoked_at`,
`last_seen_at`), igual que el resto del esquema. `created_at` sin
`DEFAULT now()` a proposito (como 0034): la hora la pone el reloj de la
aplicacion, que es el que los tests fijan.

Revision ID: 0035_mcp_oauth
Revises: 0034_execution_reservations
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035_mcp_oauth"
down_revision: str | None = "0034_execution_reservations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Vocabulario de alcances de contracts/oauth.md §3. Ampliarlo exige
# migracion: el alcance es un contrato publico, no un campo libre.
_SCOPES_CHECK = "scopes <> '' AND string_to_array(scopes, ' ') <@ ARRAY['ads:read','ads:propose']"

# RFC 8707: el indicador de recurso es una URI absoluta https, sin
# fragmento (y aqui tampoco query: el recurso es `<base>/mcp` y nada mas).
_RESOURCE_CHECK = "resource ~ '^https://[^/?#]+(/[^?#]*)?$'"

_SHA256_HEX = "^[a-f0-9]{64}$"


def upgrade() -> None:
    _upgrade_clients()
    _upgrade_authorization_requests()
    _upgrade_grants()
    _upgrade_tokens()


def downgrade() -> None:
    # Sin guarda de datos, a diferencia de 0034: aqui no se pierde nada
    # irreversible. Tirar estas tablas revoca el acceso delegado y los
    # agentes vuelven a pedir consentimiento; la huella de que ocurrio vive
    # en `decision_log`. Orden inverso al de creacion (las FKs mandan).
    op.execute("DROP TABLE oauth_tokens")
    op.execute("DROP TABLE oauth_grants")
    op.execute("DROP TABLE oauth_authorization_requests")
    op.execute("DROP TABLE oauth_clients")


def _upgrade_clients() -> None:
    op.execute(f"""
        CREATE TABLE oauth_clients (
            client_id                   TEXT PRIMARY KEY,
            client_name                 TEXT NOT NULL
                CONSTRAINT oauth_clients_client_name_check
                CHECK (char_length(client_name) BETWEEN 1 AND 100),
            redirect_uris               JSONB NOT NULL
                CONSTRAINT oauth_clients_redirect_uris_check
                CHECK (jsonb_typeof(redirect_uris) = 'array'
                       AND jsonb_array_length(redirect_uris) BETWEEN 1 AND 5),
            token_endpoint_auth_method  TEXT NOT NULL
                CONSTRAINT oauth_clients_auth_method_check
                CHECK (token_endpoint_auth_method
                       IN ('none', 'client_secret_post', 'client_secret_basic')),
            client_secret_hash          TEXT
                CONSTRAINT oauth_clients_client_secret_hash_check
                CHECK (client_secret_hash ~ '{_SHA256_HEX}'),
            grant_types                 JSONB NOT NULL
                CONSTRAINT oauth_clients_grant_types_check
                CHECK (jsonb_typeof(grant_types) = 'array'
                       AND jsonb_array_length(grant_types) >= 1),
            requested_scopes            TEXT NOT NULL
                CONSTRAINT oauth_clients_requested_scopes_check
                CHECK (requested_scopes <> ''
                       AND string_to_array(requested_scopes, ' ')
                           <@ ARRAY['ads:read','ads:propose']),
            created_at                  TIMESTAMPTZ NOT NULL,
            last_seen_at                TIMESTAMPTZ,

            CONSTRAINT oauth_clients_public_has_no_secret_check
                CHECK ((token_endpoint_auth_method = 'none') = (client_secret_hash IS NULL))
        )
    """)
    op.execute("""
        COMMENT ON TABLE oauth_clients IS
        'Clientes OAuth registrados por DCR (RFC 7591). Un cliente publico
        (token_endpoint_auth_method = none) NUNCA lleva secreto y uno
        confidencial guarda solo sha256 del suyo (threat-model.md C-44).
        No es de confianza hasta que el propietario consiente: sin concesion
        a las 24 h es podable (C-42).'
    """)
    op.execute("CREATE INDEX ix_oauth_clients_created_at ON oauth_clients (created_at)")
    op.execute(
        "CREATE UNIQUE INDEX ix_oauth_clients_secret_hash ON oauth_clients (client_secret_hash)"
    )


def _upgrade_authorization_requests() -> None:
    op.execute(rf"""
        CREATE TABLE oauth_authorization_requests (
            txn_id                 UUID PRIMARY KEY,
            client_id              TEXT NOT NULL
                                     REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
            owner_id               UUID REFERENCES owners (id) ON DELETE CASCADE,
            redirect_uri           TEXT NOT NULL
                CONSTRAINT oauth_authorization_requests_redirect_uri_check
                CHECK (redirect_uri ~ '^https://[^/?#]+(/[^#]*)?$'
                       OR redirect_uri ~
                          '^http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{{1,5}})?(/[^#]*)?$'),
            redirect_uri_explicit  BOOLEAN NOT NULL,
            code_challenge         TEXT NOT NULL
                CONSTRAINT oauth_authorization_requests_code_challenge_check
                CHECK (code_challenge ~ '^[A-Za-z0-9_-]{{43}}$'),
            client_state           TEXT
                CONSTRAINT oauth_authorization_requests_client_state_check
                CHECK (char_length(client_state) BETWEEN 1 AND 512),
            scopes                 TEXT NOT NULL
                CONSTRAINT oauth_authorization_requests_scopes_check
                CHECK ({_SCOPES_CHECK}),
            resource               TEXT NOT NULL
                CONSTRAINT oauth_authorization_requests_resource_check
                CHECK ({_RESOURCE_CHECK}),
            code_hash              TEXT
                CONSTRAINT oauth_authorization_requests_code_hash_check
                CHECK (code_hash ~ '{_SHA256_HEX}'),
            state                  TEXT NOT NULL DEFAULT 'PENDING'
                CONSTRAINT oauth_authorization_requests_state_check
                CHECK (state IN ('PENDING', 'CONSENTED', 'REDEEMED', 'DENIED', 'EXPIRED')),
            created_at             TIMESTAMPTZ NOT NULL,
            expires_at             TIMESTAMPTZ NOT NULL,
            consented_at           TIMESTAMPTZ,
            redeemed_at            TIMESTAMPTZ,

            CONSTRAINT oauth_authorization_requests_ttl_check
                CHECK (expires_at > created_at),
            CONSTRAINT oauth_authorization_requests_code_only_when_consented_check
                CHECK ((state = 'CONSENTED') = (code_hash IS NOT NULL AND redeemed_at IS NULL)),
            CONSTRAINT oauth_authorization_requests_owner_iff_consented_check
                CHECK ((consented_at IS NULL) = (owner_id IS NULL)),
            CONSTRAINT oauth_authorization_requests_consent_provenance_check
                CHECK (state NOT IN ('CONSENTED', 'REDEEMED') OR consented_at IS NOT NULL),
            CONSTRAINT oauth_authorization_requests_redeemed_check
                CHECK ((state = 'REDEEMED') = (redeemed_at IS NOT NULL)
                       AND (state <> 'REDEEMED' OR code_hash IS NOT NULL))
        )
    """)
    op.execute("""
        COMMENT ON TABLE oauth_authorization_requests IS
        'Solicitud de autorizacion en curso. El codigo es de un solo uso
        (threat-model.md C-38): vive hasheado y SOLO en estado CONSENTED --
        canjear es UPDATE ... WHERE code_hash = $1 AND state = CONSENTED
        RETURNING, y caducar una CONSENTED borra el hash. El owner_id solo
        existe si hubo consentimiento del propietario (C-40/C-41).'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ix_oauth_authorization_requests_code_hash
        ON oauth_authorization_requests (code_hash)
    """)
    op.execute("""
        CREATE INDEX ix_oauth_authorization_requests_live
        ON oauth_authorization_requests (expires_at)
        WHERE state IN ('PENDING', 'CONSENTED')
    """)
    op.execute("""
        CREATE INDEX ix_oauth_authorization_requests_client
        ON oauth_authorization_requests (client_id)
    """)
    op.execute("""
        CREATE INDEX ix_oauth_authorization_requests_owner
        ON oauth_authorization_requests (owner_id)
        WHERE owner_id IS NOT NULL
    """)


def _upgrade_grants() -> None:
    op.execute(f"""
        CREATE TABLE oauth_grants (
            grant_id              UUID PRIMARY KEY,
            owner_id              UUID NOT NULL REFERENCES owners (id) ON DELETE CASCADE,
            client_id             TEXT NOT NULL
                                    REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
            authorization_txn_id  UUID
                                    REFERENCES oauth_authorization_requests (txn_id)
                                    ON DELETE SET NULL,
            scopes                TEXT NOT NULL
                CONSTRAINT oauth_grants_scopes_check
                CHECK ({_SCOPES_CHECK}),
            resource              TEXT NOT NULL
                CONSTRAINT oauth_grants_resource_check
                CHECK ({_RESOURCE_CHECK}),
            created_at            TIMESTAMPTZ NOT NULL,
            revoked_at            TIMESTAMPTZ,
            revoked_reason        TEXT
                CONSTRAINT oauth_grants_revoked_reason_check
                CHECK (char_length(revoked_reason) BETWEEN 1 AND 200),

            CONSTRAINT oauth_grants_revocation_check
                CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL))
        )
    """)
    op.execute("""
        COMMENT ON TABLE oauth_grants IS
        'Autoridad delegada viva: propietario + cliente + alcance + recurso.
        Sobrevive a la rotacion de tokens y solo muere revocada (revoked_at
        + motivo: panel C-55, /revoke RFC 7009, reuso de codigo C-38 o de
        refresh C-43). El motivo es texto de auditoria a proposito, no un
        enum: la BD no ramifica por el.'
    """)
    op.execute("CREATE INDEX ix_oauth_grants_client ON oauth_grants (client_id)")
    op.execute("CREATE INDEX ix_oauth_grants_owner ON oauth_grants (owner_id, created_at DESC)")
    op.execute("""
        CREATE UNIQUE INDEX ix_oauth_grants_authorization_txn
        ON oauth_grants (authorization_txn_id)
    """)


def _upgrade_tokens() -> None:
    op.execute(f"""
        CREATE TABLE oauth_tokens (
            token_hash    TEXT PRIMARY KEY
                CONSTRAINT oauth_tokens_token_hash_check
                CHECK (token_hash ~ '{_SHA256_HEX}'),
            grant_id      UUID NOT NULL REFERENCES oauth_grants (grant_id) ON DELETE CASCADE,
            kind          TEXT NOT NULL
                CONSTRAINT oauth_tokens_kind_check
                CHECK (kind IN ('access', 'refresh')),
            state         TEXT NOT NULL DEFAULT 'ACTIVE'
                CONSTRAINT oauth_tokens_state_check
                CHECK (state IN ('ACTIVE', 'ROTATED', 'REVOKED')),
            issued_at     TIMESTAMPTZ NOT NULL,
            expires_at    TIMESTAMPTZ NOT NULL,
            rotated_from  TEXT
                CONSTRAINT oauth_tokens_rotated_from_check
                CHECK (rotated_from ~ '{_SHA256_HEX}')
                REFERENCES oauth_tokens (token_hash) ON DELETE SET NULL,

            CONSTRAINT oauth_tokens_ttl_check
                CHECK (expires_at > issued_at),
            CONSTRAINT oauth_tokens_rotation_is_refresh_check
                CHECK (rotated_from IS NULL OR kind = 'refresh'),
            CONSTRAINT oauth_tokens_rotated_from_not_self_check
                CHECK (rotated_from IS NULL OR rotated_from <> token_hash)
        )
    """)
    op.execute("""
        COMMENT ON TABLE oauth_tokens IS
        'Access y refresh emitidos de una concesion, SIEMPRE hasheados
        (threat-model.md C-44): la verificacion busca por sha256 del token
        presentado. ROTATED no se borra nunca -- es lo que permite detectar
        el reuso de un refresh (C-43) y revocar la familia entera.'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ix_oauth_tokens_grant_active
        ON oauth_tokens (grant_id, kind) WHERE state = 'ACTIVE'
    """)
    op.execute("""
        CREATE INDEX ix_oauth_tokens_expiry
        ON oauth_tokens (expires_at) WHERE state = 'ACTIVE'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ix_oauth_tokens_rotated_from
        ON oauth_tokens (rotated_from) WHERE rotated_from IS NOT NULL
    """)
    op.execute("CREATE INDEX ix_oauth_tokens_grant ON oauth_tokens (grant_id)")
