import { useEffect } from "react";
import type { Theme } from "@/api/schemas/settings";

/** Aplica el tema elegido en Ajustes sobre `<html data-theme>`; "system" deja que decida `prefers-color-scheme`. */
export function useApplyTheme(theme: Theme | undefined) {
  useEffect(() => {
    if (!theme || theme === "system") {
      document.documentElement.removeAttribute("data-theme");
      return;
    }
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
}
