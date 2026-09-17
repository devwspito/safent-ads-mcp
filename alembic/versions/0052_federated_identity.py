"""0052_federated_identity: login federado con Google (spec 002b,
data-model.md §Migration plan) -- `owner_federated_identities`,
`sessions.origin`/`sessions.last_federated_auth_at`,
`federated_login_transactions` y el arreglo de integridad de
`sessions.owner_id`.

Cuatro pasos, estrategia expand/contract, ninguna operacion destructiva:

1. `owner_federated_identities` (tabla nueva): identidad federada del dueno,
   atada al identificador estable del proveedor (`sub`), NUNCA al correo. PK
   `(issuer, subject)` -- un identificador de Google pertenece a un unico
   dueno -- y UNIQUE `(owner_id, issuer)` -- un dueno no queda atado a dos
   cuentas del mismo proveedor sin intervencion administrativa.
2. `sessions.origin` (DEFAULT 'password') y `sessions.last_federated_auth_at`:
   como nacio la sesion (auditoria, FR-104) y cuando se identifico por ultima
   vez ante el proveedor (frescura, NFR-104). El DEFAULT se CONSERVA en el
   estado final a proposito: 'password' es el origen MENOS capaz, asi que un
   escritor con un bug deja al dueno fuera, no dentro (fail-closed).
3. `federated_login_transactions` (tabla nueva): el salto de ida y vuelta al
   proveedor. `state` y `nonce` viven SOLO como huella sha256 (mismo criterio
   que `oauth_tokens.token_hash`/`code_hash` de 0035). Consumo atomico de un
   solo uso, fila en Postgres para sobrevivir a un reinicio de `ads-api`
   (FR-116) -- por eso no es un `state` firmado sin estado.
4. `sessions.owner_id` pasa a `ON DELETE CASCADE`. Hoy `0001_bootstrap.py:93`
   lo declara sin accion de borrado, mientras que `oauth_grants.owner_id` y
   `oauth_authorization_requests.owner_id` (0035) si la tienen: borrar un
   dueno FALLA por clave ajena y NFR-107 ("la supresion arrastra sesiones y
   concesiones") no se cumple en `main`.

El nombre de esa clave ajena se DETECTA en tiempo de ejecucion en
`pg_constraint` en vez de suponerse. En la base desplegada de la VM se
verifico que es `sessions_owner_id_fkey` (el nombre por defecto de Postgres),
pero una base creada por otro camino puede llevar otro y un `DROP CONSTRAINT`
a ciegas reventaria la migracion a mitad. Se recrea CON EL MISMO NOMBRE
detectado, asi que `downgrade()` es exactamente simetrico y ninguna
instalacion cambia de nombre por pasar por aqui.

Reglas de borrado completas tras 0052:

    DELETE FROM owners
      -> sessions                         CASCADE (paso 4)
           -> federated_login_transactions CASCADE (proposito reidentify)
      -> owner_federated_identities       CASCADE
      -> oauth_grants                     CASCADE (0035)
      -> oauth_authorization_requests     CASCADE (0035)
           -> federated_login_transactions CASCADE
      -> oauth_tokens                     (via oauth_grants, 0035)

`login_attempts` NO se borra en cascada: es el libro de intentos indexado por
correo, no por dueno, y es la prueba del bloqueo 5/15 min. Su retencion se
gobierna aparte.

Indices: solo el parcial `ix_federated_login_transactions_live`, que es el
que sirve al barrido de poda del janitor existente (`... WHERE expires_at <
:now` sobre las filas vivas). Las dos columnas de clave ajena
(`session_id`, `txn_id`) se dejan SIN indice a proposito: la tabla vive con
un punado de filas (TTL de 10 minutos, un solo dueno por despliegue, poda
periodica), asi que el barrido secuencial que paga la cascada es mas barato
que mantener dos indices en cada insercion. Si el modelo pasa a
multi-dueno, esta es la primera decision que hay que revisar.

Revision ID: 0052_federated_identity
Revises: 0051_merge_oauth_lane003
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Regla del repo: id de revision <= 32 caracteres (`0052_federated_identity` = 23).
revision: str = "0052_federated_identity"
down_revision: str | None = "0051_merge_oauth_lane003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SHA256_HEX = "^[0-9a-f]{64}$"

# Nombre esperado (verificado en la BD desplegada de la VM: `sessions_owner_id_fkey`,
# definida como `FOREIGN KEY (owner_id) REFERENCES owners(id)` sin accion de
# borrado). Solo se usa en el mensaje de error: el nombre real se detecta.
_EXPECTED_SESSIONS_OWNER_FK = "sessions_owner_id_fkey"

# `conkey` es el vector de columnas de la restriccion; compararlo contra el
# `attnum` de `owner_id` distingue esta clave ajena de cualquier otra que
# `sessions` pueda ganar en el futuro sin depender de su nombre.
_FIND_SESSIONS_OWNER_FK = """
        SELECT c.conname, c.confdeltype
          INTO fk_name, fk_delete_action
          FROM pg_constraint c
         WHERE c.conrelid = 'sessions'::regclass
           AND c.contype = 'f'
           AND c.confrelid = 'owners'::regclass
           AND c.conkey = ARRAY[(SELECT a.attnum
                                   FROM pg_attribute a
                                  WHERE a.attrelid = 'sessions'::regclass
                                    AND a.attname = 'owner_id'
                                    AND NOT a.attisdropped)];
"""


def _recreate_sessions_owner_fk(*, delete_action: str, already_applied: str) -> str:
    """`DO $$` que localiza la clave ajena de `sessions.owner_id` por su
    columna, la tira por su nombre REAL y la vuelve a crear con ese mismo
    nombre y la accion de borrado pedida.

    `already_applied` es el `confdeltype` que significa "ya esta como la
    queremos" ('c' = CASCADE, 'a' = NO ACTION): re-ejecutar la migracion
    sobre una base que ya paso por aqui no hace nada.
    """
    return f"""
    DO $$
    DECLARE
        fk_name          text;
        fk_delete_action "char";
    BEGIN
        {_FIND_SESSIONS_OWNER_FK}
        IF fk_name IS NULL THEN
            RAISE EXCEPTION
                'no se encontro la clave ajena de sessions.owner_id -> owners.id '
                '(se esperaba %); la migracion 0052 no puede continuar sin ella',
                '{_EXPECTED_SESSIONS_OWNER_FK}';
        END IF;
        IF fk_delete_action = '{already_applied}' THEN
            RETURN;
        END IF;
        EXECUTE format('ALTER TABLE sessions DROP CONSTRAINT %I', fk_name);
        EXECUTE format(
            'ALTER TABLE sessions ADD CONSTRAINT %I '
            'FOREIGN KEY (owner_id) REFERENCES owners (id){delete_action}',
            fk_name
        );
    END $$;
    """


def upgrade() -> None:
    _upgrade_federated_identities()
    _upgrade_session_origin()
    _upgrade_federated_transactions()
    _upgrade_sessions_owner_cascade()


def downgrade() -> None:
    # Orden inverso al de creacion. Deshacer no puede fallar por datos: las
    # dos tablas nuevas caen enteras y las dos columnas de `sessions` se
    # borran con ellas -- lo unico que se pierde es la identidad federada
    # atada, que se vuelve a atar por TOFU en la siguiente entrada.
    _downgrade_sessions_owner_cascade()
    op.execute("DROP TABLE federated_login_transactions")
    _downgrade_session_origin()
    op.execute("DROP TABLE owner_federated_identities")


def _upgrade_federated_identities() -> None:
    op.execute("""
        CREATE TABLE owner_federated_identities (
            owner_id          UUID        NOT NULL
                                            REFERENCES owners (id) ON DELETE CASCADE,
            issuer            TEXT        NOT NULL
                CONSTRAINT owner_federated_identities_issuer_check
                CHECK (issuer = 'https://accounts.google.com'),
            subject           TEXT        NOT NULL
                CONSTRAINT owner_federated_identities_subject_check
                CHECK (char_length(subject) BETWEEN 1 AND 255),
            email_at_binding  TEXT        NOT NULL
                CONSTRAINT owner_federated_identities_email_check
                CHECK (char_length(email_at_binding) BETWEEN 3 AND 254),
            bound_at          TIMESTAMPTZ NOT NULL,
            last_seen_at      TIMESTAMPTZ NOT NULL,

            PRIMARY KEY (issuer, subject),
            CONSTRAINT owner_federated_identities_one_per_owner UNIQUE (owner_id, issuer),
            CONSTRAINT owner_federated_identities_seen_check CHECK (last_seen_at >= bound_at)
        )
    """)
    op.execute("""
        COMMENT ON TABLE owner_federated_identities IS
        'Identidad federada del dueno: vinculo al identificador estable del
         proveedor (sub), nunca al correo. UNIQUE (owner_id, issuer) impide
         atar un dueno a dos cuentas del mismo proveedor sin intervencion
         administrativa. email_at_binding es auditoria del alta, no la clave
         de busqueda. El vocabulario cerrado de issuer es deliberado: otros
         proveedores estan fuera de alcance y abrirlo es un expand de una
         linea.'
    """)


def _upgrade_session_origin() -> None:
    # El DEFAULT se queda en el estado final (no es un apano de backfill):
    # 'password' es el origen menos capaz. Produccion no tiene todavia
    # ninguna fila en `owners`, asi que el backfill es vacio en la practica;
    # las filas preexistentes de cualquier otra instalacion quedan
    # etiquetadas 'password', lo cual NO concede nada (siguen sin poder
    # producir TOTP, exactamente como hoy).
    op.execute("ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'password'")
    op.execute("ALTER TABLE sessions ADD COLUMN last_federated_auth_at TIMESTAMPTZ")
    op.execute("""
        ALTER TABLE sessions ADD CONSTRAINT sessions_origin_check
            CHECK (origin IN ('federated', 'password', 'bridge'))
    """)
    op.execute("""
        ALTER TABLE sessions ADD CONSTRAINT sessions_federated_origin_check
            CHECK (origin <> 'federated' OR last_federated_auth_at IS NOT NULL)
    """)
    op.execute("""
        COMMENT ON COLUMN sessions.origin IS
        'Como nacio la sesion: federated | password | bridge. Es AUDITORIA, no
         autoridad -- lo que decide una accion sensible es la prueba
         presentada, no el origen. Nunca se reescribe tras la creacion.'
    """)
    op.execute("""
        COMMENT ON COLUMN sessions.last_federated_auth_at IS
        'Ultima identificacion ante el proveedor de identidad. La frescura es
         derivada (este instante + ventana de FEDERATED_IDENTIFICATION_TTL) y
         se comprueba SIEMPRE despues de que la sesion resulte activa: una
         sesion revocada o caducada no puede tener identificacion fresca.'
    """)


def _upgrade_federated_transactions() -> None:
    op.execute(f"""
        CREATE TABLE federated_login_transactions (
            state_hash   TEXT        PRIMARY KEY
                CONSTRAINT federated_login_transactions_state_hash_check
                CHECK (state_hash ~ '{_SHA256_HEX}'),
            nonce_hash   TEXT        NOT NULL
                CONSTRAINT federated_login_transactions_nonce_hash_check
                CHECK (nonce_hash ~ '{_SHA256_HEX}'),
            purpose      TEXT        NOT NULL
                CONSTRAINT federated_login_transactions_purpose_check
                CHECK (purpose IN ('login', 'reidentify')),
            session_id   UUID        REFERENCES sessions (id) ON DELETE CASCADE,
            txn_id       UUID        REFERENCES oauth_authorization_requests (txn_id)
                                       ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            consumed_at  TIMESTAMPTZ,

            CONSTRAINT federated_login_transactions_ttl_check
                CHECK (expires_at > created_at),
            CONSTRAINT federated_login_transactions_session_iff_reidentify_check
                CHECK ((purpose = 'reidentify') = (session_id IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX ix_federated_login_transactions_live
            ON federated_login_transactions (expires_at) WHERE consumed_at IS NULL
    """)
    op.execute("""
        COMMENT ON TABLE federated_login_transactions IS
        'Salto de ida y vuelta al proveedor de identidad. state y nonce viven
         SOLO hasheados (huella, como oauth_tokens/code_hash). Consumo atomico
         de un solo uso: UPDATE ... WHERE state_hash=$1 AND consumed_at IS NULL
         AND expires_at>$2 RETURNING. Sobrevive a un reinicio de ads-api
         (FR-116). Sin destino libre: la vuelta se deriva de txn_id, asi que no
         hay redireccion abierta.'
    """)


def _upgrade_sessions_owner_cascade() -> None:
    op.execute(_recreate_sessions_owner_fk(delete_action=" ON DELETE CASCADE", already_applied="c"))


def _downgrade_sessions_owner_cascade() -> None:
    # `confdeltype = 'a'` es NO ACTION, que es lo que Postgres guarda para una
    # clave ajena declarada sin accion de borrado (0001_bootstrap.py).
    op.execute(_recreate_sessions_owner_fk(delete_action="", already_applied="a"))


def _downgrade_session_origin() -> None:
    op.execute("ALTER TABLE sessions DROP CONSTRAINT sessions_federated_origin_check")
    op.execute("ALTER TABLE sessions DROP CONSTRAINT sessions_origin_check")
    op.execute("ALTER TABLE sessions DROP COLUMN last_federated_auth_at")
    op.execute("ALTER TABLE sessions DROP COLUMN origin")
