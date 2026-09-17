import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";
import { FormData, Headers, Request, Response, fetch } from "undici";
import { server } from "@/mocks/server";

// Los workers de test corren en paralelo; 1000 ms por defecto es demasiado ajustado
// bajo contención de CPU y produce falsos negativos intermitentes.
configure({ asyncUtilTimeout: 3000 });

// Dos huecos distintos de jsdom se combinan para colgar para siempre el primer test de subida
// de fichero con cuerpo real de este repo (T220, `ConversionsCard`/`POST /conversions/import`):
//
// 1. La `FormData`/`Request`/`fetch` de jsdom no son las que de verdad viajan por red (Node/
//    undici) -- `fetch(url, {body: jsdomFormData})` nunca llega a serializarse: se sustituyen
//    por las de undici, la implementación real.
// 2. `Blob`/`File` de jsdom SÍ interoperan con la `FormData`/`Request` de undici (comprobado:
//    el fichero llega al handler), pero la `Blob` de jsdom no implementa `.text()`/
//    `.arrayBuffer()` (hueco de jsdom, no de esta app) -- se añaden sobre su propio
//    `Blob.prototype` (via `FileReader`, que jsdom sí implementa entero) en vez de sustituir
//    `Blob`/`File` por los de Node: sustituirlos rompe el reconocimiento de `<input type=file>`/
//    `user.upload()`, que crean sus ficheros con la clase interna de jsdom pase lo que pase.
Object.assign(globalThis, { fetch, Headers, Request, Response, FormData });
if (typeof Blob.prototype.text !== "function") {
  Blob.prototype.text = function (this: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error as Error);
      reader.readAsText(this);
    });
  };
}
if (typeof Blob.prototype.arrayBuffer !== "function") {
  Blob.prototype.arrayBuffer = function (this: Blob): Promise<ArrayBuffer> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error as Error);
      reader.readAsArrayBuffer(this);
    });
  };
}
// undici lee el cuerpo saliente vía `.stream()`, no `.arrayBuffer()`/`.text()` -- sin esto, la
// petición sale con el fichero de tamaño 0 (metadatos sí, bytes no) en vez de colgarse o fallar
// alto, el fallo más dificil de los tres de detectar.
if (typeof Blob.prototype.stream !== "function") {
  Blob.prototype.stream = function (this: Blob): ReadableStream<Uint8Array> {
    const arrayBufferPromise = this.arrayBuffer();
    return new ReadableStream({
      async start(controller) {
        controller.enqueue(new Uint8Array(await arrayBufferPromise));
        controller.close();
      },
    });
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  // localStorage sobrevive entre tests del mismo archivo (jsdom no lo resetea solo);
  // sin esto, preferencias como la lente del cockpit se filtran de un test a otro.
  localStorage.clear();
});
afterAll(() => server.close());

// jsdom no implementa window.open — usado al reconectar una cuenta (redirección OAuth en pestaña nueva).
window.open = () => null;

// jsdom no implementa matchMedia — usado por `prefers-color-scheme`/`prefers-reduced-motion`.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
