import client from './client';

export const eventsApi = {
  getTimeline: (entityType: string, entityId: string) =>
    client.get(`/api/v1/events/timeline/${entityType}/${entityId}`),

  getAllEvents: (limit = 100, offset = 0, since?: number) =>
    client.get('/api/v1/events/all', { params: { limit, offset, since } }),

  getEntityState: (entityType: string, entityId: string, at?: number) =>
    client.get(`/api/v1/events/state/${entityType}/${entityId}`, { params: { at } }),

  getRegistry: () => client.get('/api/v1/events/registry'),

  getGraph: (entityType?: string, entityId?: string, limit = 200) =>
    client.get('/api/v1/events/graph', {
      params: { entity_type: entityType, entity_id: entityId, limit },
    }),

  getStats: () => client.get('/api/v1/events/stats'),

  getTransitionMatrix: (entityType?: string) =>
    client.get('/api/v1/events/transition-matrix', {
      params: { entity_type: entityType },
    }),

  // Committee
  getCommitteeStatus: () => client.get('/api/v1/events/committee/status'),
  getCommitteeHistory: (limit = 20) =>
    client.get('/api/v1/events/committee/history', { params: { limit } }),

  // Automation
  getAutomationStatus: () => client.get('/api/v1/events/automation/status'),
  getAutomationHistory: (limit = 20) =>
    client.get('/api/v1/events/automation/history', { params: { limit } }),

  // Predictions
  getPredictionsStatus: () => client.get('/api/v1/events/predictions/status'),
  predictNext: (currentState: string, prevState?: string) =>
    client.get('/api/v1/events/predictions/predict', {
      params: { current_state: currentState, prev_state: prevState },
    }),
  getMarkovMatrix: () => client.get('/api/v1/events/predictions/matrix'),

  // Patterns
  getPatternsStatus: () => client.get('/api/v1/events/patterns/status'),
  getTopPatterns: (n = 20) =>
    client.get('/api/v1/events/patterns/top', { params: { n } }),
};
