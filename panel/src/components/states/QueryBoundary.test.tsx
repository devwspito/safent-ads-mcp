import { expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryBoundary } from './QueryBoundary';

const props = { isLoading: false, isError: false, onRetry: vi.fn(), isEmpty: (data: string[]) => !data.length, emptyTitle: 'No hay campañas', children: (data: string[]) => <p>{data.join(',')}</p> };

it('does not turn a missing response into a verified empty account', async () => {
  render(<QueryBoundary {...props} data={undefined} />);
  expect(screen.queryByText('No hay campañas')).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('respuesta verificable');
  await userEvent.click(screen.getByRole('button', { name: 'Reintentar' }));
  expect(props.onRetry).toHaveBeenCalledOnce();
});
it('distinguishes verified empty data from loading and results', () => {
  const view = render(<QueryBoundary {...props} data={[]} />);
  expect(screen.getByRole('status')).toHaveTextContent('No hay campañas');
  view.rerender(<QueryBoundary {...props} isLoading data={undefined} />);
  expect(screen.getByRole('status')).toHaveAttribute('aria-busy', 'true');
  view.rerender(<QueryBoundary {...props} data={['Campaña de prueba']} />);
  expect(screen.getByText('Campaña de prueba')).toBeInTheDocument();
});
