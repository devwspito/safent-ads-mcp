import { MOCK_TOTP_CODE } from "../fixtures/businesses";
import {
  isMockFederatedPresenceFresh,
  isMockTotpPresenceFresh,
  markMockTotpPresenceFresh,
} from "../fixtures/mcpOauth";

/**
 * Identificación fresca (contracts/federated-login.md §2): la federada es de sesión (vale para
 * cualquier acción dentro de la ventana); el TOTP es POR ACCIÓN — `actionKey` identifica la
 * acción exacta (`revoke:${grantId}`, `approve:${txnId}`, `caps:${accountId}`) y un código dado
 * para una no sirve para otra, aunque sea del mismo tipo con un identificador distinto.
 *
 * Copia única a propósito: dos superficies sensibles que decidieran la frescura por su cuenta
 * podrían divergir, y la que divergiera de menos sería un agujero.
 */
export function hasMockFreshIdentification(request: Request, actionKey: string): boolean {
  if (request.headers.get("X-Reauth-Token") === MOCK_TOTP_CODE) {
    markMockTotpPresenceFresh(actionKey);
    return true;
  }
  return isMockTotpPresenceFresh(actionKey) || isMockFederatedPresenceFresh();
}
