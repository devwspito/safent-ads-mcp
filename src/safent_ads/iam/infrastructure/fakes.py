"""Dobles de las primitivas criptograficas de `iam` y del proveedor de
identidad federada, para que los tests de casos de uso corran en
milisegundos sin pagar el coste real de Argon2id ni salir a la red.
Nunca se cablean en `composition/`: solo los importan los tests."""

from __future__ import annotations

from safent_ads.iam.application.ports import FederatedIdentityClaims
from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.federated_identity import FederatedIssuer, FederatedSubject
from safent_ads.iam.domain.federated_transaction import ReferenceHash

_DEFAULT_SUBJECT = "google-subject-000"
_DEFAULT_EMAIL = "dueno@example.com"


class FakePasswordHasher:
    """`hash`/`verify` deterministas y reversibles -- explicitamente
    inseguros, solo para pruebas de orquestacion."""

    def hash(self, password: str) -> str:
        return f"fake${password}"

    def verify(self, password: str, password_hash: str) -> bool:
        return password_hash == f"fake${password}"


class FakeTotpCipher:
    def encrypt(self, secret: str) -> bytes:
        return secret.encode("utf-8")

    def decrypt(self, blob: bytes) -> str:
        return blob.decode("utf-8")


class FakeFederatedIdentityProvider:
    """Doble en memoria de `FederatedIdentityProvider` (spec 002b T024): la
    ida y la vuelta contra Google sin red ni `monkeypatch` de httpx.

    Guarda el `nonce` en claro de cada salto (tal y como lo veria Google
    dentro del `id_token`) y exige que su huella case con
    `expected_nonce_hash` en el canje, igual que el adaptador real
    (`iam/application/federated_id_token.py`). Un doble que aceptara
    cualquier huella dejaria pasar tests que el proveedor de verdad
    rechazaria.
    """

    def __init__(
        self,
        *,
        issuer: FederatedIssuer = FederatedIssuer.GOOGLE,
        subject: FederatedSubject | None = None,
        email: Email | None = None,
        email_verified: bool = True,
        authorization_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth",
    ) -> None:
        self.issuer = issuer
        self.subject = subject or FederatedSubject(_DEFAULT_SUBJECT)
        self.email = email or Email(_DEFAULT_EMAIL)
        self.email_verified = email_verified
        self._authorization_endpoint = authorization_endpoint
        self.failure: Exception | None = None
        self.exchanges: list[str] = []
        self._last_nonce: str | None = None

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        self._last_nonce = nonce
        return (
            f"{self._authorization_endpoint}?response_type=code&scope=openid+email"
            f"&prompt=select_account&state={state}&nonce={nonce}&redirect_uri={redirect_uri}"
        )

    async def exchange_code(
        self, *, code: str, redirect_uri: str, expected_nonce_hash: ReferenceHash
    ) -> FederatedIdentityClaims:
        del redirect_uri
        if self.failure is not None:
            raise self.failure
        self.exchanges.append(code)
        if self._last_nonce is None or ReferenceHash.of(self._last_nonce) != expected_nonce_hash:
            raise ValueError("el canje exige el nonce del salto que lo abrio")
        return FederatedIdentityClaims(
            issuer=self.issuer,
            subject=self.subject,
            email=self.email,
            email_verified=self.email_verified,
        )
