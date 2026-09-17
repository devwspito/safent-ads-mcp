// Explicit browser-test helpers. Not imported by main.tsx or production build.
import { delay, http, HttpResponse } from 'msw';
import { worker } from '../src/mocks/browser';
import { queryClient } from '../src/api/queryClient';
import { buildCockpit } from '../src/mocks/fixtures/cockpit';
import { DEFAULT_MOCK_BUSINESS } from '../src/mocks/fixtures/businesses';
import { API_BASE } from '../src/mocks/handlers/apiBase';

export async function cockpitState(mode: 'error' | 'empty' | 'loading' | 'normal') {
  worker.resetHandlers();
  if (mode !== 'normal') worker.use(http.get(`${API_BASE}/cockpit`, async () => {
    if (mode === 'loading') await delay(3000);
    if (mode === 'error') return HttpResponse.json({ error: { code: 'UNAVAILABLE', message: 'Fixture: respuesta temporalmente indisponible' } }, { status: 503 });
    const data = buildCockpit(DEFAULT_MOCK_BUSINESS.business_id, '7d');
    return HttpResponse.json(mode === 'empty' ? { ...data, rows: [] } : data);
  }));
  await queryClient.cancelQueries({ queryKey: ['cockpit'] });
  queryClient.removeQueries({ queryKey: ['cockpit'] });
}
