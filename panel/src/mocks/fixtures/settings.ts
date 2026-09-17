/** `contracts/rest-api.md` §Conexiones, Telegram y ajustes — `timezone`/`currency` los dicta la plataforma: se muestran, no se editan. */
interface SettingsRecord {
  timezone: string;
  currency: string;
  active_hours: { start: string; end: string };
  digest_hour: string;
  theme: "light" | "dark" | "system";
}

const record: SettingsRecord = {
  timezone: "Europe/Madrid",
  currency: "EUR",
  active_hours: { start: "08:00", end: "21:00" },
  digest_hour: "09:00",
  theme: "system",
};

export function getSettings() {
  return { business_id: "biz_ejemplo", ...record };
}

export function updateSettings(update: Pick<SettingsRecord, "active_hours" | "digest_hour" | "theme">) {
  Object.assign(record, update);
  return getSettings();
}
