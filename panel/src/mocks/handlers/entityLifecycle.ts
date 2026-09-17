import { http, HttpResponse } from "msw";
import { DEFAULT_MOCK_BUSINESS } from "../fixtures/businesses";
import {
  ENTITY_ACTION_GRACE_SECONDS,
  findCockpitRow,
  getCockpitEntityStatus,
  recordCockpitEntityAction,
  setCockpitEntityStatus,
} from "../fixtures/cockpit";
import { getKillSwitchState } from "../fixtures/killSwitch";
import { API_BASE } from "./apiBase";

function denied(code: "BRAKE_ENGAGED" | "NOT_CONTROLLABLE" | "ALREADY_IN_TARGET_STATE", message: string) {
  return HttpResponse.json({ error: { code, message } }, { status: 409 });
}

const NOT_FOUND = HttpResponse.json({ error: { code: "NOT_FOUND", message: "No encontrado." } }, { status: 404 });

function brakeEngagedFor(platformAccountId: string): boolean {
  const state = getKillSwitchState(DEFAULT_MOCK_BUSINESS.business_id);
  return state.by_account.find((item) => item.platform_account_id === platformAccountId)?.engaged ?? false;
}

let execCounter = 0;
function nextExecutionId(prefix: string): string {
  execCounter += 1;
  return `exec_${prefix}_${execCounter}`;
}

/**
 * `POST /entities/{entity_ref}/pause|resume|delete` — mismos códigos 404/409/502 que el backend
 * real (`execution/application/entity_lifecycle_actions.py`, `composition/execution_rest.py`).
 */
export const entityLifecycleHandlers = [
  http.post(`${API_BASE}/entities/:entityRef/pause`, ({ params }) => {
    const entityRef = decodeURIComponent(String(params.entityRef));
    const row = findCockpitRow(entityRef);
    if (!row) return NOT_FOUND;
    if (!row.is_controllable) return denied("NOT_CONTROLLABLE", "No se puede cambiar desde aquí.");
    const status = getCockpitEntityStatus(entityRef, row.status);
    if (status !== "ACTIVE") return denied("ALREADY_IN_TARGET_STATE", "Ya estaba en ese estado.");
    if (brakeEngagedFor(row.platform_account_id)) return denied("BRAKE_ENGAGED", "Los cambios de esta cuenta están parados.");
    setCockpitEntityStatus(entityRef, "PAUSED");
    const executionId = nextExecutionId("pause");
    recordCockpitEntityAction(executionId, entityRef, "ACTIVE");
    return HttpResponse.json({ execution_id: executionId, undo_deadline: new Date(Date.now() + ENTITY_ACTION_GRACE_SECONDS * 1000).toISOString() });
  }),

  http.post(`${API_BASE}/entities/:entityRef/resume`, ({ params }) => {
    const entityRef = decodeURIComponent(String(params.entityRef));
    const row = findCockpitRow(entityRef);
    if (!row) return NOT_FOUND;
    if (!row.is_controllable) return denied("NOT_CONTROLLABLE", "No se puede cambiar desde aquí.");
    const status = getCockpitEntityStatus(entityRef, row.status);
    if (status !== "PAUSED") return denied("ALREADY_IN_TARGET_STATE", "Ya estaba en ese estado.");
    if (brakeEngagedFor(row.platform_account_id)) return denied("BRAKE_ENGAGED", "Los cambios de esta cuenta están parados.");
    setCockpitEntityStatus(entityRef, "ACTIVE");
    const executionId = nextExecutionId("resume");
    recordCockpitEntityAction(executionId, entityRef, "PAUSED");
    return HttpResponse.json({ execution_id: executionId, undo_deadline: new Date(Date.now() + ENTITY_ACTION_GRACE_SECONDS * 1000).toISOString() });
  }),

  http.post(`${API_BASE}/entities/:entityRef/delete`, ({ params }) => {
    const entityRef = decodeURIComponent(String(params.entityRef));
    const row = findCockpitRow(entityRef);
    if (!row) return NOT_FOUND;
    if (!row.is_controllable) return denied("NOT_CONTROLLABLE", "No se puede cambiar desde aquí.");
    const status = getCockpitEntityStatus(entityRef, row.status);
    if (status === "REMOVED") return denied("ALREADY_IN_TARGET_STATE", "Ya estaba en ese estado.");
    if (brakeEngagedFor(row.platform_account_id)) return denied("BRAKE_ENGAGED", "Los cambios de esta cuenta están parados.");
    setCockpitEntityStatus(entityRef, "REMOVED");
    return HttpResponse.json({ execution_id: nextExecutionId("delete") });
  }),
];
