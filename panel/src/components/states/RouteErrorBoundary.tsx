import { Component, type ReactNode } from "react";
import { ErrorState } from "./ErrorState";

/** A failed route bundle/render must not take navigation or the brake with it. */
export class RouteErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() { return { failed: true }; }

  render() {
    if (this.state.failed) return <ErrorState message="Esta vista no se ha podido abrir. Puedes ir a otra sección o recargar el panel. No se repetirá ninguna operación automáticamente." onRetry={() => window.location.reload()} />;
    return this.props.children;
  }
}
