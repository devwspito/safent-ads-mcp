import { useEffect } from "react";

interface ShortcutHandlers {
  onNavigate: (index: number) => void;
  onOpenPalette: () => void;
  onOpenHelp: () => void;
  onEscape: () => void;
  onOpenBrake: () => void;
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/**
 * Atajos globales — panel-interaction-spec.md §1.1:
 * `1`-`4` destino, `Cmd/Ctrl+K` búsqueda, `?` atajos, `Esc` cierra la capa superior, `Shift+F` abre "Parar cambios".
 */
export function useKeyboardShortcuts({ onNavigate, onOpenPalette, onOpenHelp, onEscape, onOpenBrake }: ShortcutHandlers) {
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || document.querySelector('[role="dialog"]')) return;
      if (event.key === "Escape") {
        onEscape();
        return;
      }

      const isTyping = isTypingTarget(event.target);

      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onOpenPalette();
        return;
      }

      if (isTyping || event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.key === "?") {
        onOpenHelp();
        return;
      }

      if (event.shiftKey && event.key.toLowerCase() === "f") {
        onOpenBrake();
        return;
      }

      if (/^[1-4]$/.test(event.key)) {
        onNavigate(Number(event.key) - 1);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onNavigate, onOpenPalette, onOpenHelp, onEscape, onOpenBrake]);
}
