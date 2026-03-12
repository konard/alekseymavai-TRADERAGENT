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

  // Leaderboard
  getLeaderboard: () => client.get('/api/v1/events/leaderboard'),
  getAllocations: () => client.get('/api/v1/events/allocations'),
  getAnomalies: (limit = 50) =>
    client.get('/api/v1/events/anomalies', { params: { limit } }),

  // Position Sizing
  calculatePositionSize: (portfolioValue: number, baseRiskPct = 0.02, confidence = 0.5, patternScore = 0.0) =>
    client.get('/api/v1/events/position-sizing/calculate', {
      params: { portfolio_value: portfolioValue, base_risk_pct: baseRiskPct, committee_confidence: confidence, pattern_score: patternScore },
    }),
  getPositionSizerStatus: () => client.get('/api/v1/events/position-sizing/status'),

  // Correlation
  getCorrelationMatrix: () => client.get('/api/v1/events/correlation/matrix'),
  checkCorrelation: (pair: string, direction = 'long') =>
    client.get('/api/v1/events/correlation/check', { params: { pair, direction } }),
  getCorrelationStatus: () => client.get('/api/v1/events/correlation/status'),

  // Monte Carlo
  getMonteCarloStatus: () => client.get('/api/v1/events/monte-carlo/status'),
  runMonteCarlo: (simulations = 1000, horizon = 252) =>
    client.post('/api/v1/events/monte-carlo/run', null, { params: { simulations, horizon } }),

  // Replay
  getReplayStatus: () => client.get('/api/v1/events/replay/status'),

  // Evolution
  getEvolutionStatus: () => client.get('/api/v1/events/evolution/status'),
  getEvolutionBest: () => client.get('/api/v1/events/evolution/best'),
};
