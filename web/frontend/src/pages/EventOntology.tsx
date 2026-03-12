import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Card } from '../components/common/Card';
import { PageTransition } from '../components/common/PageTransition';
import { useEventStore, type DomainEvent } from '../stores/eventStore';
import { eventsApi } from '../api/events';
import { WebSocketClient } from '../api/websocket';

/* ─────────────────── constants ─────────────────── */

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const ENTITY_COLORS: Record<string, string> = {
  position: '#3b82f6',
  regime: '#8b5cf6',
  strategy: '#10b981',
  risk: '#ef4444',
  portfolio: '#f59e0b',
  bot: '#6b7280',
};

const ENTITY_LABELS: Record<string, string> = {
  position: 'Position',
  regime: 'Regime',
  strategy: 'Strategy',
  risk: 'Risk',
  portfolio: 'Portfolio',
  bot: 'Bot',
};

const TABS = [
  { key: 'timeline', label: 'Timeline' },
  { key: 'graph', label: 'Event Graph' },
  { key: 'state', label: 'State Projections' },
  { key: 'registry', label: 'Registry & Matrix' },
  { key: 'committee', label: 'Committee' },
  { key: 'automation', label: 'Automation' },
  { key: 'predictions', label: 'Predictions' },
  { key: 'patterns', label: 'Patterns' },
  { key: 'leaderboard', label: 'Leaderboard' },
  { key: 'risk-tools', label: 'Risk Tools' },
  { key: 'evolution', label: 'Evolution' },
] as const;

type TabKey = (typeof TABS)[number]['key'];

function entityColor(type: string): string {
  return ENTITY_COLORS[type.toLowerCase()] || '#6b7280';
}

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDate(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function abbreviate(eventType: string): string {
  return eventType
    .split('_')
    .map((w) => w[0]?.toUpperCase() || '')
    .join('');
}

/* ─────────────────── main page ─────────────────── */

export function EventOntology() {
  const [activeTab, setActiveTab] = useState<TabKey>('timeline');

  return (
    <PageTransition>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold text-text font-[Manrope]">Event Ontology</h2>
          <p className="text-xs text-text-muted mt-1">Real-time event monitoring & causal analysis</p>
        </div>
        <EventStats />
      </div>

      {/* tabs */}
      <div className="flex gap-1 bg-surface border border-border rounded-lg p-1 mb-6 w-fit">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 text-sm rounded-md transition-all duration-200 ${
              activeTab === tab.key
                ? 'bg-primary text-white shadow-lg shadow-primary/25'
                : 'text-text-muted hover:text-text hover:bg-surface-hover'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'timeline' && <TimelineTab />}
      {activeTab === 'graph' && <GraphTab />}
      {activeTab === 'state' && <StateTab />}
      {activeTab === 'registry' && <RegistryTab />}
      {activeTab === 'committee' && <CommitteeTab />}
      {activeTab === 'automation' && <AutomationTab />}
      {activeTab === 'predictions' && <PredictionsTab />}
      {activeTab === 'patterns' && <PatternsTab />}
      {activeTab === 'leaderboard' && <LeaderboardTab />}
      {activeTab === 'risk-tools' && <RiskToolsTab />}
      {activeTab === 'evolution' && <EvolutionTab />}
    </PageTransition>
  );
}

/* ─────────────────── event stats badge ─────────────────── */

function EventStats() {
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    eventsApi
      .getStats()
      .then((r) => setStats(r.data))
      .catch(() => {});
  }, []);

  if (!stats) return null;

  return (
    <div className="flex items-center gap-3">
      {typeof stats.total_events === 'number' && (
        <div className="text-right">
          <p className="text-xs text-text-muted">Total Events</p>
          <p className="text-lg font-bold text-text">{(stats.total_events as number).toLocaleString()}</p>
        </div>
      )}
      <div className="w-3 h-3 rounded-full bg-profit animate-pulse" />
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 1: TIMELINE
   ═══════════════════════════════════════════════════════════ */

function TimelineTab() {
  const { events, filteredEntityType, isPaused, addEvent, setFilter, togglePause, clearEvents } = useEventStore();
  const scrollRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocketClient | null>(null);

  // bootstrap: fetch recent events
  useEffect(() => {
    eventsApi
      .getAllEvents(100)
      .then((r) => {
        const items: DomainEvent[] = Array.isArray(r.data) ? r.data : r.data?.events || [];
        items.reverse().forEach(addEvent);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // websocket for real-time
  useEffect(() => {
    const ws = new WebSocketClient(API_BASE_URL);
    wsRef.current = ws;
    const token = localStorage.getItem('access_token') || '';
    ws.connect(token);
    const unsub = ws.onMessage((msg) => {
      if (msg.type === 'domain_event' && msg.data) {
        addEvent(msg.data as unknown as DomainEvent);
      }
    });
    return () => {
      unsub();
      ws.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // auto-scroll
  useEffect(() => {
    if (!isPaused && scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [events.length, isPaused]);

  const filtered = useMemo(() => {
    if (!filteredEntityType) return events;
    return events.filter((e) => e.entity_type === filteredEntityType);
  }, [events, filteredEntityType]);

  // build cause lookup for dotted lines
  const causeMap = useMemo(() => {
    const m = new Map<string, number>();
    filtered.forEach((e, i) => m.set(e.event_id, i));
    return m;
  }, [filtered]);

  return (
    <div className="space-y-4">
      {/* toolbar */}
      <div className="flex items-center gap-3 flex-wrap">
        <select
          value={filteredEntityType || ''}
          onChange={(e) => setFilter(e.target.value || null)}
          className="bg-surface border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-primary"
        >
          <option value="">All entities</option>
          {Object.entries(ENTITY_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>

        <button
          onClick={togglePause}
          className={`px-4 py-2 text-sm rounded-lg border transition-colors ${
            isPaused
              ? 'border-loss text-loss hover:bg-loss/10'
              : 'border-profit text-profit hover:bg-profit/10'
          }`}
        >
          {isPaused ? '▶ Resume' : '⏸ Pause'}
        </button>

        <button
          onClick={clearEvents}
          className="px-4 py-2 text-sm rounded-lg border border-border text-text-muted hover:text-text hover:bg-surface-hover transition-colors"
        >
          Clear
        </button>

        <span className="text-xs text-text-muted ml-auto">
          {filtered.length} events {isPaused && '(paused)'}
        </span>
      </div>

      {/* event feed */}
      <div ref={scrollRef} className="max-h-[calc(100vh-280px)] overflow-y-auto space-y-2 pr-1">
        <AnimatePresence initial={false}>
          {filtered.map((event, idx) => (
            <TimelineCard key={event.event_id} event={event} causeMap={causeMap} index={idx} />
          ))}
        </AnimatePresence>

        {filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center py-20 text-text-muted">
            <div className="text-4xl mb-3 opacity-30">~</div>
            <p className="text-sm">No events yet</p>
            <p className="text-xs mt-1">Events will appear here in real time</p>
          </div>
        )}
      </div>
    </div>
  );
}

function TimelineCard({
  event,
  causeMap,
}: {
  event: DomainEvent;
  causeMap: Map<string, number>;
  index: number;
}) {
  const color = entityColor(event.entity_type);
  const hasCauses = event.causes && event.causes.length > 0;
  const causeExists = hasCauses && event.causes.some((c) => causeMap.has(c));

  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 20 }}
      transition={{ duration: 0.2 }}
      className="relative"
    >
      {/* causal chain dotted line */}
      {causeExists && (
        <div
          className="absolute left-6 -top-2 w-px h-2"
          style={{ borderLeft: `2px dotted ${color}40` }}
        />
      )}

      <div
        className="bg-surface border border-border rounded-lg p-4 hover:bg-surface-hover transition-colors group"
        style={{ borderLeftWidth: '3px', borderLeftColor: color }}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            {/* entity type badge */}
            <span
              className="shrink-0 text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full"
              style={{ backgroundColor: `${color}20`, color }}
            >
              {event.entity_type}
            </span>

            {/* event type */}
            <span className="text-sm font-medium text-text truncate">{event.event_type}</span>
          </div>

          {/* timestamp */}
          <span className="text-xs text-text-muted shrink-0 font-mono">{fmtTs(event.ts)}</span>
        </div>

        {/* entity info */}
        <div className="mt-2 flex items-center gap-4 text-xs text-text-muted">
          <span>
            entity: <span className="text-text">{event.entity_id}</span>
          </span>
          {event.bot_name && (
            <span>
              bot: <span className="text-text">{event.bot_name}</span>
            </span>
          )}
          {event.priority > 0 && (
            <span>
              priority: <span className="text-orange">{event.priority}</span>
            </span>
          )}
        </div>

        {/* data summary */}
        {event.data && Object.keys(event.data).length > 0 && (
          <div className="mt-2 bg-background rounded-md p-2 text-xs font-mono text-text-muted overflow-x-auto max-h-20 overflow-y-auto">
            {Object.entries(event.data)
              .slice(0, 6)
              .map(([k, v]) => (
                <div key={k}>
                  <span className="text-text-muted">{k}:</span>{' '}
                  <span className="text-text">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                </div>
              ))}
            {Object.keys(event.data).length > 6 && (
              <div className="text-text-muted opacity-50">... +{Object.keys(event.data).length - 6} more</div>
            )}
          </div>
        )}

        {/* causal links */}
        {hasCauses && (
          <div className="mt-2 flex items-center gap-1 text-[10px] text-text-muted">
            <span style={{ color: `${color}90` }}>caused by:</span>
            {event.causes.map((c) => (
              <span key={c} className="font-mono px-1 py-0.5 rounded bg-background" style={{ color: `${color}80` }}>
                {c.slice(0, 8)}
              </span>
            ))}
          </div>
        )}

        {event.enables && event.enables.length > 0 && (
          <div className="mt-1 flex items-center gap-1 text-[10px] text-text-muted">
            <span style={{ color: `${color}90` }}>enables:</span>
            {event.enables.map((e) => (
              <span key={e} className="font-mono px-1 py-0.5 rounded bg-background" style={{ color: `${color}80` }}>
                {e.slice(0, 8)}
              </span>
            ))}
          </div>
        )}
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 2: EVENT GRAPH
   ═══════════════════════════════════════════════════════════ */

interface GraphNode {
  id: string;
  event_type: string;
  entity_type: string;
  ts: number;
  x: number;
  y: number;
}

interface GraphEdge {
  from: string;
  to: string;
}

function GraphTab() {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<DomainEvent | null>(null);
  const [loading, setLoading] = useState(true);
  const [entityFilter, setEntityFilter] = useState<string>('');
  const svgRef = useRef<SVGSVGElement>(null);
  const [viewBox, setViewBox] = useState({ x: 0, y: 0, w: 1200, h: 700 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  useEffect(() => {
    setLoading(true);
    eventsApi
      .getGraph(entityFilter || undefined, undefined, 200)
      .then((r) => {
        const data = r.data;
        const rawNodes: DomainEvent[] = data.nodes || data.events || [];
        const rawEdges: GraphEdge[] = data.edges || [];

        // layout: force-directed-like circular placement
        const laid = layoutNodes(rawNodes);
        setNodes(laid);
        setEdges(rawEdges.length > 0 ? rawEdges : buildEdgesFromCauses(rawNodes));
      })
      .catch(() => {
        setNodes([]);
        setEdges([]);
      })
      .finally(() => setLoading(false));
  }, [entityFilter]);

  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      setSelected(node);
      // fetch full event data
      eventsApi
        .getTimeline(node.entity_type, node.id)
        .then((r) => {
          const evts = Array.isArray(r.data) ? r.data : r.data?.events || [];
          const found = evts.find((e: DomainEvent) => e.event_id === node.id);
          setSelectedEvent(found || null);
        })
        .catch(() => setSelectedEvent(null));
    },
    [],
  );

  // pan handlers
  const onMouseDown = (e: React.MouseEvent) => {
    if (e.target === svgRef.current || (e.target as SVGElement).tagName === 'rect') {
      setDragging(true);
      setDragStart({ x: e.clientX, y: e.clientY });
    }
  };
  const onMouseMove = (e: React.MouseEvent) => {
    if (!dragging) return;
    const dx = e.clientX - dragStart.x;
    const dy = e.clientY - dragStart.y;
    setViewBox((v) => ({ ...v, x: v.x - dx * (v.w / 1200), y: v.y - dy * (v.h / 700) }));
    setDragStart({ x: e.clientX, y: e.clientY });
  };
  const onMouseUp = () => setDragging(false);
  const onWheel = (e: React.WheelEvent) => {
    const factor = e.deltaY > 0 ? 1.1 : 0.9;
    setViewBox((v) => ({
      x: v.x + (v.w * (1 - factor)) / 2,
      y: v.y + (v.h * (1 - factor)) / 2,
      w: v.w * factor,
      h: v.h * factor,
    }));
  };

  const nodeMap = useMemo(() => {
    const m = new Map<string, GraphNode>();
    nodes.forEach((n) => m.set(n.id, n));
    return m;
  }, [nodes]);

  return (
    <div className="flex gap-4 h-[calc(100vh-280px)]">
      {/* graph canvas */}
      <div className="flex-1 bg-surface border border-border rounded-xl overflow-hidden relative">
        {/* entity filter */}
        <div className="absolute top-3 left-3 z-10">
          <select
            value={entityFilter}
            onChange={(e) => setEntityFilter(e.target.value)}
            className="bg-background border border-border rounded-lg px-3 py-1.5 text-xs text-text focus:outline-none focus:border-primary"
          >
            <option value="">All entities</option>
            {Object.entries(ENTITY_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>

        {/* zoom controls */}
        <div className="absolute top-3 right-3 z-10 flex flex-col gap-1">
          <button
            onClick={() =>
              setViewBox((v) => ({
                x: v.x + v.w * 0.05,
                y: v.y + v.h * 0.05,
                w: v.w * 0.9,
                h: v.h * 0.9,
              }))
            }
            className="w-7 h-7 bg-background border border-border rounded text-text text-xs hover:bg-surface-hover"
          >
            +
          </button>
          <button
            onClick={() =>
              setViewBox((v) => ({
                x: v.x - v.w * 0.05,
                y: v.y - v.h * 0.05,
                w: v.w * 1.1,
                h: v.h * 1.1,
              }))
            }
            className="w-7 h-7 bg-background border border-border rounded text-text text-xs hover:bg-surface-hover"
          >
            -
          </button>
          <button
            onClick={() => setViewBox({ x: 0, y: 0, w: 1200, h: 700 })}
            className="w-7 h-7 bg-background border border-border rounded text-text text-[10px] hover:bg-surface-hover"
          >
            fit
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center h-full text-text-muted text-sm">Loading graph...</div>
        ) : nodes.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-text-muted">
            <div className="text-3xl mb-2 opacity-30">~</div>
            <p className="text-sm">No graph data available</p>
          </div>
        ) : (
          <svg
            ref={svgRef}
            className="w-full h-full cursor-grab active:cursor-grabbing"
            viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={onMouseUp}
            onMouseLeave={onMouseUp}
            onWheel={onWheel}
          >
            <rect x={viewBox.x} y={viewBox.y} width={viewBox.w} height={viewBox.h} fill="transparent" />

            {/* edges */}
            <defs>
              <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
                <polygon points="0 0, 8 3, 0 6" fill="#30363d" />
              </marker>
            </defs>
            {edges.map((edge, i) => {
              const from = nodeMap.get(edge.from);
              const to = nodeMap.get(edge.to);
              if (!from || !to) return null;
              return (
                <line
                  key={i}
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  stroke="#30363d"
                  strokeWidth="1.5"
                  markerEnd="url(#arrowhead)"
                  opacity={0.6}
                />
              );
            })}

            {/* nodes */}
            {nodes.map((node) => {
              const color = entityColor(node.entity_type);
              const isSelected = selected?.id === node.id;
              return (
                <g
                  key={node.id}
                  onClick={() => handleNodeClick(node)}
                  className="cursor-pointer"
                >
                  {/* glow on select */}
                  {isSelected && <circle cx={node.x} cy={node.y} r={26} fill={`${color}20`} />}
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={20}
                    fill={`${color}30`}
                    stroke={color}
                    strokeWidth={isSelected ? 2.5 : 1.5}
                  />
                  <text
                    x={node.x}
                    y={node.y + 1}
                    textAnchor="middle"
                    dominantBaseline="central"
                    fill={color}
                    fontSize="8"
                    fontWeight="bold"
                    fontFamily="monospace"
                  >
                    {abbreviate(node.event_type)}
                  </text>
                  {/* tooltip label below */}
                  <text
                    x={node.x}
                    y={node.y + 32}
                    textAnchor="middle"
                    fill="#8b949e"
                    fontSize="7"
                    fontFamily="sans-serif"
                  >
                    {node.event_type.length > 18 ? node.event_type.slice(0, 18) + '..' : node.event_type}
                  </text>
                </g>
              );
            })}
          </svg>
        )}

        {/* legend */}
        <div className="absolute bottom-3 left-3 flex items-center gap-3 text-[10px] text-text-muted">
          {Object.entries(ENTITY_LABELS).map(([k, v]) => (
            <div key={k} className="flex items-center gap-1">
              <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: entityColor(k) }} />
              {v}
            </div>
          ))}
        </div>
      </div>

      {/* side panel */}
      <AnimatePresence>
        {selected && (
          <motion.div
            initial={{ opacity: 0, x: 40 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 40 }}
            className="w-80 shrink-0"
          >
            <Card className="h-full overflow-y-auto">
              <div className="flex items-center justify-between mb-4">
                <h4 className="text-sm font-semibold text-text">Event Details</h4>
                <button onClick={() => setSelected(null)} className="text-text-muted hover:text-text text-lg">
                  x
                </button>
              </div>

              <div className="space-y-3 text-xs">
                <KV label="Event ID" value={selected.id} mono />
                <KV label="Event Type" value={selected.event_type} />
                <KV
                  label="Entity Type"
                  value={
                    <span style={{ color: entityColor(selected.entity_type) }}>
                      {selected.entity_type}
                    </span>
                  }
                />
                <KV label="Timestamp" value={fmtDate(selected.ts)} mono />

                {selectedEvent && (
                  <>
                    <KV label="Bot" value={selectedEvent.bot_name || '-'} />
                    <KV label="Priority" value={String(selectedEvent.priority)} />

                    {selectedEvent.causes.length > 0 && (
                      <div>
                        <span className="text-text-muted">Causes:</span>
                        <div className="mt-1 space-y-1">
                          {selectedEvent.causes.map((c) => (
                            <span key={c} className="block font-mono text-[10px] text-text bg-background rounded px-2 py-1">
                              {c}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                    {selectedEvent.enables.length > 0 && (
                      <div>
                        <span className="text-text-muted">Enables:</span>
                        <div className="mt-1 space-y-1">
                          {selectedEvent.enables.map((e) => (
                            <span key={e} className="block font-mono text-[10px] text-text bg-background rounded px-2 py-1">
                              {e}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                    {selectedEvent.data && Object.keys(selectedEvent.data).length > 0 && (
                      <div>
                        <span className="text-text-muted">Data:</span>
                        <div className="mt-1 bg-background rounded-md p-2 font-mono text-[10px] max-h-60 overflow-y-auto">
                          {Object.entries(selectedEvent.data).map(([k, v]) => (
                            <div key={k} className="py-0.5">
                              <span className="text-text-muted">{k}: </span>
                              <span className="text-text">
                                {typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
            </Card>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function layoutNodes(events: DomainEvent[]): GraphNode[] {
  if (events.length === 0) return [];

  const centerX = 600;
  const centerY = 350;
  const maxRadius = 280;

  // group by entity_type, then radial layout
  const byType: Record<string, DomainEvent[]> = {};
  events.forEach((e) => {
    const t = e.entity_type || 'unknown';
    (byType[t] ||= []).push(e);
  });

  const types = Object.keys(byType);
  const nodes: GraphNode[] = [];

  types.forEach((type, ti) => {
    const items = byType[type];
    const sectorAngle = (2 * Math.PI) / types.length;
    const baseAngle = ti * sectorAngle;

    items.forEach((e, ei) => {
      const rings = Math.ceil(items.length / 8);
      const ring = Math.floor(ei / 8);
      const posInRing = ei % 8;
      const ringItemCount = Math.min(8, items.length - ring * 8);
      const r = maxRadius * 0.3 + (maxRadius * 0.7 * (ring + 1)) / (rings + 1);
      const angle = baseAngle + (sectorAngle * (posInRing + 0.5)) / ringItemCount;

      nodes.push({
        id: e.event_id,
        event_type: e.event_type,
        entity_type: e.entity_type,
        ts: e.ts,
        x: centerX + r * Math.cos(angle),
        y: centerY + r * Math.sin(angle),
      });
    });
  });

  return nodes;
}

function buildEdgesFromCauses(events: DomainEvent[]): GraphEdge[] {
  const ids = new Set(events.map((e) => e.event_id));
  const edges: GraphEdge[] = [];
  events.forEach((e) => {
    if (e.causes) {
      e.causes.forEach((causeId) => {
        if (ids.has(causeId)) {
          edges.push({ from: causeId, to: e.event_id });
        }
      });
    }
  });
  return edges;
}

/* ═══════════════════════════════════════════════════════════
   TAB 3: STATE PROJECTIONS
   ═══════════════════════════════════════════════════════════ */

const STATE_ENTITIES = [
  { type: 'position', label: 'Position', defaultId: 'default' },
  { type: 'regime', label: 'Regime', defaultId: 'global' },
  { type: 'portfolio', label: 'Portfolio', defaultId: 'main' },
  { type: 'strategy', label: 'Strategy', defaultId: 'default' },
];

function StateTab() {
  const [states, setStates] = useState<Record<string, Record<string, unknown>>>({});
  const [loading, setLoading] = useState(true);
  const [replayTs, setReplayTs] = useState<number | undefined>(undefined);
  const [tsRange, setTsRange] = useState({ min: 0, max: 0 });

  const fetchStates = useCallback(
    (at?: number) => {
      setLoading(true);
      Promise.all(
        STATE_ENTITIES.map((e) =>
          eventsApi
            .getEntityState(e.type, e.defaultId, at)
            .then((r) => ({ type: e.type, data: r.data }))
            .catch(() => ({ type: e.type, data: null })),
        ),
      ).then((results) => {
        const s: Record<string, Record<string, unknown>> = {};
        results.forEach((r) => {
          if (r.data) s[r.type] = r.data as Record<string, unknown>;
        });
        setStates(s);
        setLoading(false);
      });
    },
    [],
  );

  // initial fetch + determine time range
  useEffect(() => {
    fetchStates();
    eventsApi
      .getStats()
      .then((r) => {
        const data = r.data as Record<string, unknown>;
        if (data.first_event_ts && data.last_event_ts) {
          setTsRange({
            min: data.first_event_ts as number,
            max: data.last_event_ts as number,
          });
        }
      })
      .catch(() => {});
  }, [fetchStates]);

  const handleSliderChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = Number(e.target.value);
    if (val === tsRange.max) {
      setReplayTs(undefined);
      fetchStates();
    } else {
      setReplayTs(val);
      fetchStates(val);
    }
  };

  return (
    <div className="space-y-6">
      {/* replay slider */}
      {tsRange.max > 0 && (
        <Card>
          <div className="flex items-center gap-4">
            <span className="text-xs text-text-muted whitespace-nowrap">Replay</span>
            <input
              type="range"
              min={tsRange.min}
              max={tsRange.max}
              step={1}
              value={replayTs ?? tsRange.max}
              onChange={handleSliderChange}
              className="flex-1 h-1.5 rounded-lg appearance-none cursor-pointer accent-primary"
              style={{ background: 'linear-gradient(to right, #640075, #30363d)' }}
            />
            <span className="text-xs font-mono text-text-muted whitespace-nowrap w-40 text-right">
              {replayTs ? fmtDate(replayTs) : 'current'}
            </span>
          </div>
        </Card>
      )}

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {STATE_ENTITIES.map((e) => (
            <Card key={e.type}>
              <div className="animate-pulse space-y-3">
                <div className="h-4 bg-border rounded w-1/3" />
                <div className="h-3 bg-border rounded w-2/3" />
                <div className="h-3 bg-border rounded w-1/2" />
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {STATE_ENTITIES.map((entity) => {
            const data = states[entity.type];
            const color = entityColor(entity.type);
            return (
              <Card key={entity.type}>
                <div className="flex items-center gap-2 mb-4">
                  <div className="w-3 h-3 rounded-full" style={{ backgroundColor: color }} />
                  <h4 className="text-sm font-semibold text-text">{entity.label}</h4>
                  <span className="text-[10px] font-mono text-text-muted ml-auto">{entity.type}/{entity.defaultId}</span>
                </div>

                {data ? (
                  <div className="space-y-1.5">
                    {Object.entries(data).map(([k, v]) => (
                      <div key={k} className="flex items-start justify-between gap-2 py-1 border-b border-border/50 last:border-0">
                        <span className="text-xs text-text-muted shrink-0">{k}</span>
                        <span className="text-xs text-text font-mono text-right break-all">
                          {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-text-muted">No state data</p>
                )}
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 4: REGISTRY & MATRIX
   ═══════════════════════════════════════════════════════════ */

interface RegistryEntry {
  event_type: string;
  label: string;
  entity_type: string;
  causes: string[];
  enables: string[];
}

interface TransitionEntry {
  from: string;
  to: string;
  count: number;
  probability: number;
}

function RegistryTab() {
  const [subTab, setSubTab] = useState<'registry' | 'matrix'>('registry');
  const [registry, setRegistry] = useState<RegistryEntry[]>([]);
  const [matrix, setMatrix] = useState<TransitionEntry[]>([]);
  const [matrixFilter, setMatrixFilter] = useState<string>('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      eventsApi.getRegistry().catch(() => ({ data: [] })),
      eventsApi.getTransitionMatrix().catch(() => ({ data: [] })),
    ]).then(([regRes, matRes]) => {
      const regData = Array.isArray(regRes.data) ? regRes.data : regRes.data?.event_types || [];
      setRegistry(regData);
      const matData = Array.isArray(matRes.data) ? matRes.data : matRes.data?.transitions || [];
      setMatrix(matData);
      setLoading(false);
    });
  }, []);

  // build heatmap data
  const heatmapData = useMemo(() => {
    const filtered = matrixFilter ? matrix.filter((m) => m.from.includes(matrixFilter) || m.to.includes(matrixFilter)) : matrix;
    const types = Array.from(new Set([...filtered.map((m) => m.from), ...filtered.map((m) => m.to)]));
    const grid: Record<string, Record<string, number>> = {};
    types.forEach((from) => {
      grid[from] = {};
      types.forEach((to) => {
        grid[from][to] = 0;
      });
    });
    filtered.forEach((m) => {
      if (grid[m.from]) grid[m.from][m.to] = m.probability;
    });
    return { types, grid };
  }, [matrix, matrixFilter]);

  return (
    <div className="space-y-4">
      {/* sub-tabs */}
      <div className="flex gap-2">
        <button
          onClick={() => setSubTab('registry')}
          className={`px-4 py-2 text-sm rounded-lg transition-colors ${
            subTab === 'registry' ? 'bg-primary/20 text-primary' : 'text-text-muted hover:text-text hover:bg-surface-hover'
          }`}
        >
          Event Registry
        </button>
        <button
          onClick={() => setSubTab('matrix')}
          className={`px-4 py-2 text-sm rounded-lg transition-colors ${
            subTab === 'matrix' ? 'bg-primary/20 text-primary' : 'text-text-muted hover:text-text hover:bg-surface-hover'
          }`}
        >
          Transition Matrix
        </button>
      </div>

      {loading ? (
        <Card>
          <div className="animate-pulse space-y-3">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-4 bg-border rounded" style={{ width: `${60 + Math.random() * 30}%` }} />
            ))}
          </div>
        </Card>
      ) : subTab === 'registry' ? (
        <RegistryPanel registry={registry} />
      ) : (
        <MatrixPanel heatmapData={heatmapData} matrixFilter={matrixFilter} setMatrixFilter={setMatrixFilter} />
      )}
    </div>
  );
}

function RegistryPanel({ registry }: { registry: RegistryEntry[] }) {
  if (registry.length === 0) {
    return (
      <Card>
        <div className="flex flex-col items-center justify-center py-12 text-text-muted">
          <div className="text-3xl mb-2 opacity-30">~</div>
          <p className="text-sm">No registry data available</p>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border">
              <th className="text-left py-3 px-2 text-text-muted font-medium uppercase tracking-wider">Event Type</th>
              <th className="text-left py-3 px-2 text-text-muted font-medium uppercase tracking-wider">Label</th>
              <th className="text-left py-3 px-2 text-text-muted font-medium uppercase tracking-wider">Entity</th>
              <th className="text-left py-3 px-2 text-text-muted font-medium uppercase tracking-wider">Causes</th>
              <th className="text-left py-3 px-2 text-text-muted font-medium uppercase tracking-wider">Enables</th>
            </tr>
          </thead>
          <tbody>
            {registry.map((entry) => {
              const color = entityColor(entry.entity_type);
              return (
                <tr key={entry.event_type} className="border-b border-border/30 hover:bg-surface-hover transition-colors">
                  <td className="py-2.5 px-2 font-mono text-text">{entry.event_type}</td>
                  <td className="py-2.5 px-2 text-text-muted">{entry.label}</td>
                  <td className="py-2.5 px-2">
                    <span
                      className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full"
                      style={{ backgroundColor: `${color}20`, color }}
                    >
                      {entry.entity_type}
                    </span>
                  </td>
                  <td className="py-2.5 px-2">
                    <div className="flex flex-wrap gap-1">
                      {entry.causes.map((c) => (
                        <span key={c} className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-background text-text-muted">
                          {c}
                        </span>
                      ))}
                      {entry.causes.length === 0 && <span className="text-text-muted opacity-50">-</span>}
                    </div>
                  </td>
                  <td className="py-2.5 px-2">
                    <div className="flex flex-wrap gap-1">
                      {entry.enables.map((e) => (
                        <span key={e} className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-background text-text-muted">
                          {e}
                        </span>
                      ))}
                      {entry.enables.length === 0 && <span className="text-text-muted opacity-50">-</span>}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function MatrixPanel({
  heatmapData,
  matrixFilter,
  setMatrixFilter,
}: {
  heatmapData: { types: string[]; grid: Record<string, Record<string, number>> };
  matrixFilter: string;
  setMatrixFilter: (v: string) => void;
}) {
  const { types, grid } = heatmapData;

  if (types.length === 0) {
    return (
      <Card>
        <div className="flex flex-col items-center justify-center py-12 text-text-muted">
          <div className="text-3xl mb-2 opacity-30">~</div>
          <p className="text-sm">No transition data available</p>
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <input
          type="text"
          placeholder="Filter event types..."
          value={matrixFilter}
          onChange={(e) => setMatrixFilter(e.target.value)}
          className="bg-surface border border-border rounded-lg px-3 py-2 text-sm text-text placeholder-text-muted focus:outline-none focus:border-primary w-64"
        />
        {matrixFilter && (
          <button onClick={() => setMatrixFilter('')} className="text-xs text-text-muted hover:text-text">
            Clear
          </button>
        )}
      </div>

      <Card>
        <div className="overflow-x-auto">
          <div className="inline-block min-w-full">
            <table className="text-[10px]">
              <thead>
                <tr>
                  <th className="p-1.5 text-text-muted font-medium sticky left-0 bg-surface z-10 min-w-[120px]">
                    from \ to
                  </th>
                  {types.map((t) => (
                    <th
                      key={t}
                      className="p-1.5 text-text-muted font-medium"
                      style={{ writingMode: 'vertical-rl', textOrientation: 'mixed', maxWidth: '30px' }}
                    >
                      <span className="block max-h-[100px] overflow-hidden">{t}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {types.map((from) => (
                  <tr key={from}>
                    <td className="p-1.5 font-mono text-text-muted sticky left-0 bg-surface z-10 whitespace-nowrap">
                      {from}
                    </td>
                    {types.map((to) => {
                      const prob = grid[from]?.[to] || 0;
                      const intensity = Math.min(prob, 1);
                      return (
                        <td key={to} className="p-0.5">
                          <div
                            className="w-6 h-6 rounded-sm flex items-center justify-center text-[8px] font-mono transition-colors"
                            style={{
                              backgroundColor:
                                prob > 0
                                  ? `rgba(100, 0, 117, ${0.15 + intensity * 0.7})`
                                  : 'rgba(48, 54, 61, 0.3)',
                              color: prob > 0.3 ? '#e6edf3' : prob > 0 ? '#8b949e' : 'transparent',
                            }}
                            title={`${from} -> ${to}: ${(prob * 100).toFixed(1)}%`}
                          >
                            {prob > 0 ? (prob * 100).toFixed(0) : ''}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* color legend */}
        <div className="mt-4 flex items-center gap-2 text-[10px] text-text-muted">
          <span>0%</span>
          <div className="flex h-3 rounded overflow-hidden">
            {Array.from({ length: 10 }).map((_, i) => (
              <div
                key={i}
                className="w-4"
                style={{ backgroundColor: `rgba(100, 0, 117, ${0.15 + (i / 10) * 0.7})` }}
              />
            ))}
          </div>
          <span>100%</span>
        </div>
      </Card>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 5: INVESTMENT COMMITTEE
   ═══════════════════════════════════════════════════════════ */

function CommitteeTab() {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [decisions, setDecisions] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      eventsApi.getCommitteeStatus().catch(() => ({ data: null })),
      eventsApi.getCommitteeHistory(20).catch(() => ({ data: { decisions: [] } })),
    ]).then(([statusRes, histRes]) => {
      setStatus(statusRes.data);
      setDecisions((histRes.data as Record<string, unknown>)?.decisions as Record<string, unknown>[] || []);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return <Card><div className="animate-pulse space-y-3"><div className="h-4 bg-border rounded w-1/3" /><div className="h-3 bg-border rounded w-2/3" /></div></Card>;
  }

  const experts = (status?.experts || []) as Record<string, unknown>[];
  const totalSessions = (status?.total_sessions || 0) as number;
  const verdictDist = (status?.verdict_distribution || {}) as Record<string, number>;

  return (
    <div className="space-y-6">
      {/* Expert panel */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        {experts.map((exp) => (
          <Card key={String(exp.name)}>
            <div className="flex items-center gap-2 mb-2">
              <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: '#8b5cf6' }} />
              <span className="text-sm font-semibold text-text">{String(exp.name)}</span>
            </div>
            <p className="text-xs text-text-muted">{String(exp.role)}</p>
            <p className="text-xs text-text-muted mt-1">Weight: <span className="text-text">{String(exp.weight)}</span></p>
          </Card>
        ))}
      </div>

      {/* Stats */}
      <div className="flex gap-6 text-sm">
        <div>Sessions: <span className="font-bold text-text">{totalSessions}</span></div>
        <div className="text-profit">Approve: {verdictDist.approve || 0}</div>
        <div className="text-loss">Reject: {verdictDist.reject || 0}</div>
        <div className="text-text-muted">Defer: {verdictDist.defer || 0}</div>
      </div>

      {/* Decision history */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Recent Decisions</h4>
        {decisions.length === 0 ? (
          <p className="text-xs text-text-muted">No decisions yet</p>
        ) : (
          <div className="space-y-3">
            {decisions.map((d, i) => {
              const verdict = String(d.verdict);
              const color = verdict === 'approve' ? '#10b981' : verdict === 'reject' ? '#ef4444' : '#6b7280';
              return (
                <div key={i} className="border border-border rounded-lg p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold uppercase px-2 py-0.5 rounded-full" style={{ backgroundColor: `${color}20`, color }}>
                      {verdict}
                    </span>
                    <span className="text-xs text-text-muted font-mono">
                      score: {String(d.score)} | {fmtTs(d.ts as number)}
                    </span>
                  </div>
                  <p className="text-xs text-text-muted mt-2">{String(d.reasoning)}</p>
                  {Array.isArray(d.votes) && (
                    <div className="mt-2 flex gap-2 flex-wrap">
                      {(d.votes as Record<string, unknown>[]).map((v, vi) => (
                        <span key={vi} className="text-[10px] px-2 py-0.5 rounded bg-background text-text-muted">
                          {String(v.expert)}: <span style={{ color: String(v.verdict) === 'approve' ? '#10b981' : String(v.verdict) === 'reject' ? '#ef4444' : '#6b7280' }}>{String(v.verdict)}</span> ({String(v.confidence)})
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 6: AUTOMATION RULES
   ═══════════════════════════════════════════════════════════ */

function AutomationTab() {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [history, setHistory] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      eventsApi.getAutomationStatus().catch(() => ({ data: null })),
      eventsApi.getAutomationHistory(20).catch(() => ({ data: { history: [] } })),
    ]).then(([statusRes, histRes]) => {
      setStatus(statusRes.data);
      setHistory((histRes.data as Record<string, unknown>)?.history as Record<string, unknown>[] || []);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return <Card><div className="animate-pulse space-y-3"><div className="h-4 bg-border rounded w-1/3" /></div></Card>;
  }

  const rules = (status?.rules || []) as Record<string, unknown>[];
  const totalFires = (status?.total_fires || 0) as number;

  return (
    <div className="space-y-6">
      <div className="flex gap-6 text-sm">
        <div>Rules: <span className="font-bold text-text">{rules.length}</span></div>
        <div>Active: <span className="font-bold text-profit">{status?.active_rules as number || 0}</span></div>
        <div>Total fires: <span className="font-bold text-text">{totalFires}</span></div>
        <div>Loss streak: <span className="font-bold text-loss">{status?.consecutive_losses as number || 0}</span></div>
      </div>

      {/* Rules */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Automation Rules</h4>
        <div className="space-y-2">
          {rules.map((r, i) => (
            <div key={i} className="flex items-center justify-between border border-border rounded-lg p-3">
              <div>
                <div className="flex items-center gap-2">
                  <div className={`w-2 h-2 rounded-full ${r.enabled ? 'bg-profit' : 'bg-border'}`} />
                  <span className="text-xs font-semibold text-text">{String(r.name)}</span>
                </div>
                <p className="text-[10px] text-text-muted mt-1">{String(r.description)}</p>
              </div>
              <div className="text-right text-xs text-text-muted">
                <div>trigger: <span className="font-mono text-text">{String(r.trigger)}</span></div>
                <div>fires: {String(r.fire_count)} | cd: {String(r.cooldown)}s</div>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* History */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Recent Fires</h4>
        {history.length === 0 ? (
          <p className="text-xs text-text-muted">No automation events fired yet</p>
        ) : (
          <div className="space-y-2">
            {history.map((h, i) => (
              <div key={i} className="flex items-center justify-between text-xs border-b border-border/30 py-2 last:border-0">
                <div>
                  <span className="font-semibold text-text">{String(h.rule)}</span>
                  <span className="text-text-muted ml-2">via {String(h.trigger_event)}</span>
                </div>
                <span className="text-text-muted font-mono">{h.ts ? fmtTs(h.ts as number) : '-'}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 7: PREDICTIONS (Markov Model)
   ═══════════════════════════════════════════════════════════ */

function PredictionsTab() {
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);
  const [matrixData, setMatrixData] = useState<Record<string, unknown> | null>(null);
  const [predInput, setPredInput] = useState('');
  const [prediction, setPrediction] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      eventsApi.getPredictionsStatus().catch(() => ({ data: null })),
      eventsApi.getMarkovMatrix().catch(() => ({ data: null })),
    ]).then(([statsRes, matRes]) => {
      setStats(statsRes.data);
      setMatrixData(matRes.data);
      setLoading(false);
    });
  }, []);

  const handlePredict = () => {
    if (!predInput) return;
    eventsApi.predictNext(predInput).then((r) => setPrediction(r.data)).catch(() => {});
  };

  if (loading) {
    return <Card><div className="animate-pulse space-y-3"><div className="h-4 bg-border rounded w-1/3" /></div></Card>;
  }

  const states = (stats?.states || []) as string[];
  const stationary = (matrixData?.stationary || {}) as Record<string, number>;

  return (
    <div className="space-y-6">
      {/* Stats */}
      <div className="flex gap-6 text-sm flex-wrap">
        <div>States: <span className="font-bold text-text">{stats?.total_states as number || 0}</span></div>
        <div>Transitions: <span className="font-bold text-text">{stats?.total_transitions as number || 0}</span></div>
        <div>Entities: <span className="font-bold text-text">{stats?.entities_tracked as number || 0}</span></div>
        <div>Predictions: <span className="font-bold text-text">{stats?.predictions_made as number || 0}</span></div>
      </div>

      {/* Predict tool */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-3">Predict Next State</h4>
        <div className="flex gap-2">
          <select
            value={predInput}
            onChange={(e) => setPredInput(e.target.value)}
            className="bg-background border border-border rounded-lg px-3 py-2 text-xs text-text focus:outline-none focus:border-primary flex-1"
          >
            <option value="">Select current state...</option>
            {states.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <button onClick={handlePredict} className="px-4 py-2 bg-primary text-white text-xs rounded-lg hover:bg-primary/80 transition-colors">
            Predict
          </button>
        </div>

        {prediction && (
          <div className="mt-4 bg-background rounded-lg p-3">
            <div className="flex items-center gap-3 mb-2">
              <span className="text-xs text-text-muted">Predicted:</span>
              <span className="text-sm font-bold text-primary">{String(prediction.predicted_state)}</span>
              <span className="text-xs text-text-muted">P={String(prediction.probability)}</span>
            </div>
            {prediction.horizon_seconds && (
              <p className="text-xs text-text-muted">ETA: {Math.round(prediction.horizon_seconds as number)}s</p>
            )}
            <p className="text-[10px] text-text-muted mt-1">basis: {String(prediction.basis)}</p>
            {Array.isArray(prediction.alternatives) && (prediction.alternatives as Record<string, unknown>[]).length > 0 && (
              <div className="mt-2 flex gap-2 flex-wrap">
                {(prediction.alternatives as Record<string, unknown>[]).map((a, i) => (
                  <span key={i} className="text-[10px] px-2 py-0.5 rounded bg-surface text-text-muted">
                    {String(a.state)}: {String(a.probability)}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Stationary distribution */}
      {Object.keys(stationary).length > 0 && (
        <Card>
          <h4 className="text-sm font-semibold text-text mb-3">Stationary Distribution</h4>
          <p className="text-[10px] text-text-muted mb-3">Long-run proportion of time in each state</p>
          <div className="space-y-2">
            {Object.entries(stationary)
              .sort(([, a], [, b]) => b - a)
              .map(([state, prob]) => (
                <div key={state} className="flex items-center gap-3">
                  <span className="text-xs text-text-muted w-40 truncate font-mono">{state}</span>
                  <div className="flex-1 h-4 bg-background rounded overflow-hidden">
                    <div
                      className="h-full rounded transition-all duration-300"
                      style={{ width: `${prob * 100}%`, backgroundColor: '#640075' }}
                    />
                  </div>
                  <span className="text-xs text-text-muted w-12 text-right">{(prob * 100).toFixed(1)}%</span>
                </div>
              ))}
          </div>
        </Card>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 8: PATTERN LEARNER
   ═══════════════════════════════════════════════════════════ */

function PatternsTab() {
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);
  const [patterns, setPatterns] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      eventsApi.getPatternsStatus().catch(() => ({ data: null })),
      eventsApi.getTopPatterns(20).catch(() => ({ data: { patterns: [] } })),
    ]).then(([statsRes, patRes]) => {
      setStats(statsRes.data);
      setPatterns((patRes.data as Record<string, unknown>)?.patterns as Record<string, unknown>[] || []);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return <Card><div className="animate-pulse space-y-3"><div className="h-4 bg-border rounded w-1/3" /></div></Card>;
  }

  return (
    <div className="space-y-6">
      {/* Stats */}
      <div className="flex gap-6 text-sm flex-wrap">
        <div>Patterns: <span className="font-bold text-text">{stats?.total_patterns as number || 0}</span></div>
        <div>Observations: <span className="font-bold text-text">{stats?.total_observations as number || 0}</span></div>
        <div className="text-profit">Profitable: {stats?.profitable_patterns as number || 0}</div>
        <div className="text-loss">Unprofitable: {stats?.unprofitable_patterns as number || 0}</div>
        <div>Fired: <span className="font-bold text-text">{stats?.patterns_fired as number || 0}</span></div>
      </div>

      {/* Pattern table */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Top Patterns (by confidence)</h4>
        {patterns.length === 0 ? (
          <p className="text-xs text-text-muted">No patterns learned yet. Patterns accumulate as trades close.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-2 px-2 text-text-muted font-medium">Pattern</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium">Trades</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium">Win Rate</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium">Confidence</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium">Avg PnL</th>
                </tr>
              </thead>
              <tbody>
                {patterns.map((p, i) => {
                  const wr = Number(p.win_rate || 0);
                  const wrColor = wr >= 0.55 ? '#10b981' : wr <= 0.45 ? '#ef4444' : '#6b7280';
                  return (
                    <tr key={i} className="border-b border-border/30 hover:bg-surface-hover transition-colors">
                      <td className="py-2 px-2 font-mono text-text">{String(p.name || p.pattern_id)}</td>
                      <td className="py-2 px-2 text-right text-text">{String(p.occurrences)}</td>
                      <td className="py-2 px-2 text-right font-mono" style={{ color: wrColor }}>
                        {(wr * 100).toFixed(1)}%
                      </td>
                      <td className="py-2 px-2 text-right text-text-muted">{Number(p.confidence || 0).toFixed(3)}</td>
                      <td className="py-2 px-2 text-right font-mono" style={{ color: Number(p.avg_pnl || 0) >= 0 ? '#10b981' : '#ef4444' }}>
                        {Number(p.avg_pnl || 0).toFixed(2)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 9: EXPERT LEADERBOARD
   ═══════════════════════════════════════════════════════════ */

interface LeaderboardExpert {
  rank: number;
  name: string;
  role: string;
  weight: number;
  base_weight: number;
  accuracy: number;
  correct: number;
  total: number;
  regime_accuracy: Record<string, number>;
  weight_history: number[];
}

interface AllocationData {
  allocations: Record<string, number>;
  regime: string;
  persistence_probability: number;
  updated_at: number | null;
}

interface AnomalyEntry {
  ts: number;
  type: string;
  severity: string;
  message: string;
  details?: Record<string, unknown>;
}

const REGIME_LABELS: Record<string, string> = {
  bull_trend: 'Bull',
  bear_trend: 'Bear',
  sideways: 'Side',
  volatile: 'Vol',
  accumulation: 'Accum',
  distribution: 'Distr',
  breakout: 'Break',
  mean_reversion: 'MR',
};

function LeaderboardTab() {
  const [experts, setExperts] = useState<LeaderboardExpert[]>([]);
  const [regimeTypes, setRegimeTypes] = useState<string[]>([]);
  const [allocations, setAllocations] = useState<AllocationData | null>(null);
  const [anomalies, setAnomalies] = useState<AnomalyEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      eventsApi.getLeaderboard().catch(() => ({ data: { experts: [], regime_types: [] } })),
      eventsApi.getAllocations().catch(() => ({ data: null })),
      eventsApi.getAnomalies().catch(() => ({ data: { anomalies: [] } })),
    ]).then(([lbRes, allocRes, anomRes]) => {
      const lbData = lbRes.data as Record<string, unknown>;
      setExperts((lbData?.experts as LeaderboardExpert[]) || []);
      setRegimeTypes((lbData?.regime_types as string[]) || []);
      setAllocations(allocRes.data as AllocationData | null);
      setAnomalies(((anomRes.data as Record<string, unknown>)?.anomalies as AnomalyEntry[]) || []);
      setLoading(false);
    });
  }, []);

  if (loading) {
    return (
      <Card>
        <div className="animate-pulse space-y-3">
          <div className="h-4 bg-border rounded w-1/3" />
          <div className="h-3 bg-border rounded w-2/3" />
          <div className="h-3 bg-border rounded w-1/2" />
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      {/* Section 1: Expert Rankings */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Expert Rankings</h4>
        {experts.length === 0 ? (
          <p className="text-xs text-text-muted">No expert data yet. Rankings appear after committee sessions run.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-2 px-2 text-text-muted font-medium uppercase tracking-wider">#</th>
                  <th className="text-left py-2 px-2 text-text-muted font-medium uppercase tracking-wider">Expert</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium uppercase tracking-wider">Weight</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium uppercase tracking-wider">Accuracy</th>
                  <th className="text-right py-2 px-2 text-text-muted font-medium uppercase tracking-wider">Correct / Total</th>
                  <th className="text-left py-2 px-2 text-text-muted font-medium uppercase tracking-wider">Best Regime</th>
                  <th className="text-left py-2 px-2 text-text-muted font-medium uppercase tracking-wider">Worst Regime</th>
                </tr>
              </thead>
              <tbody>
                {experts.map((exp) => {
                  const acc = Number(exp.accuracy || 0);
                  const accColor = acc > 0.6 ? '#10b981' : acc >= 0.4 ? '#6b7280' : '#ef4444';
                  const regimeEntries = Object.entries(exp.regime_accuracy || {});
                  const bestRegime = regimeEntries.length > 0
                    ? regimeEntries.reduce((a, b) => (Number(b[1]) > Number(a[1]) ? b : a))
                    : null;
                  const worstRegime = regimeEntries.length > 0
                    ? regimeEntries.reduce((a, b) => (Number(b[1]) < Number(a[1]) ? b : a))
                    : null;

                  return (
                    <tr key={exp.name} className="border-b border-border/30 hover:bg-surface-hover transition-colors">
                      <td className="py-2.5 px-2 font-bold text-text">{exp.rank}</td>
                      <td className="py-2.5 px-2">
                        <div className="text-text font-semibold">{exp.name}</div>
                        <div className="text-[10px] text-text-muted">{exp.role}</div>
                      </td>
                      <td className="py-2.5 px-2 text-right font-mono text-text">{Number(exp.weight).toFixed(2)}</td>
                      <td className="py-2.5 px-2 text-right font-mono font-bold" style={{ color: accColor }}>
                        {(acc * 100).toFixed(1)}%
                      </td>
                      <td className="py-2.5 px-2 text-right text-text-muted font-mono">
                        {exp.correct} / {exp.total}
                      </td>
                      <td className="py-2.5 px-2">
                        {bestRegime ? (
                          <span className="text-[10px] px-2 py-0.5 rounded-full bg-profit/10 text-profit">
                            {REGIME_LABELS[bestRegime[0]] || bestRegime[0]} ({(Number(bestRegime[1]) * 100).toFixed(0)}%)
                          </span>
                        ) : (
                          <span className="text-text-muted opacity-50">-</span>
                        )}
                      </td>
                      <td className="py-2.5 px-2">
                        {worstRegime ? (
                          <span className="text-[10px] px-2 py-0.5 rounded-full bg-loss/10 text-loss">
                            {REGIME_LABELS[worstRegime[0]] || worstRegime[0]} ({(Number(worstRegime[1]) * 100).toFixed(0)}%)
                          </span>
                        ) : (
                          <span className="text-text-muted opacity-50">-</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Section 2: Weight Evolution */}
      {experts.length > 0 && (
        <Card>
          <h4 className="text-sm font-semibold text-text mb-4">Weight Evolution</h4>
          <p className="text-[10px] text-text-muted mb-3">Current weight vs base weight per expert</p>
          <div className="space-y-3">
            {experts.map((exp) => {
              const current = Number(exp.weight);
              const base = Number(exp.base_weight);
              const delta = current - base;
              const maxWeight = Math.max(...experts.map((e) => Math.max(Number(e.weight), Number(e.base_weight))), 1);
              const barWidthCurrent = (current / maxWeight) * 100;
              const barWidthBase = (base / maxWeight) * 100;
              const deltaColor = delta > 0 ? '#10b981' : delta < 0 ? '#ef4444' : '#6b7280';

              return (
                <div key={exp.name} className="flex items-center gap-3">
                  <span className="text-xs text-text-muted w-32 truncate">{exp.name}</span>
                  <div className="flex-1 space-y-1">
                    {/* Base weight bar */}
                    <div className="h-2.5 bg-background rounded overflow-hidden relative">
                      <div
                        className="h-full rounded opacity-30"
                        style={{ width: `${barWidthBase}%`, backgroundColor: '#6b7280' }}
                      />
                    </div>
                    {/* Current weight bar */}
                    <div className="h-2.5 bg-background rounded overflow-hidden">
                      <div
                        className="h-full rounded transition-all duration-300"
                        style={{ width: `${barWidthCurrent}%`, backgroundColor: deltaColor }}
                      />
                    </div>
                  </div>
                  <div className="text-right w-20">
                    <span className="text-xs font-mono text-text">{current.toFixed(2)}</span>
                    <span className="text-[10px] font-mono ml-1" style={{ color: deltaColor }}>
                      ({delta >= 0 ? '+' : ''}{delta.toFixed(2)})
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-4 text-[10px] text-text-muted">
            <div className="flex items-center gap-1">
              <div className="w-4 h-2 rounded opacity-30" style={{ backgroundColor: '#6b7280' }} />
              <span>Base weight</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="w-4 h-2 rounded" style={{ backgroundColor: '#10b981' }} />
              <span>Current (increased)</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="w-4 h-2 rounded" style={{ backgroundColor: '#ef4444' }} />
              <span>Current (decreased)</span>
            </div>
          </div>
        </Card>
      )}

      {/* Section 3: Regime Heatmap */}
      {experts.length > 0 && regimeTypes.length > 0 && (
        <Card>
          <h4 className="text-sm font-semibold text-text mb-4">Regime Accuracy Heatmap</h4>
          <p className="text-[10px] text-text-muted mb-3">Per-expert accuracy across market regimes</p>
          <div className="overflow-x-auto">
            <table className="text-[10px]">
              <thead>
                <tr>
                  <th className="p-1.5 text-text-muted font-medium text-left min-w-[120px]">Expert</th>
                  {regimeTypes.map((regime) => (
                    <th key={regime} className="p-1.5 text-text-muted font-medium text-center">
                      {REGIME_LABELS[regime] || regime}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {experts.map((exp) => (
                  <tr key={exp.name}>
                    <td className="p-1.5 font-mono text-text-muted whitespace-nowrap">{exp.name}</td>
                    {regimeTypes.map((regime) => {
                      const accuracy = Number(exp.regime_accuracy?.[regime] || 0);
                      return (
                        <td key={regime} className="p-0.5">
                          <div
                            className="w-12 h-8 rounded-sm flex items-center justify-center text-[10px] font-mono transition-colors"
                            style={{
                              backgroundColor: accuracy > 0
                                ? `rgba(100, 0, 117, ${0.15 + accuracy * 0.7})`
                                : 'rgba(48, 54, 61, 0.3)',
                              color: accuracy > 0.6 ? '#e6edf3' : accuracy > 0 ? '#8b949e' : 'transparent',
                            }}
                            title={`${exp.name} / ${regime}: ${(accuracy * 100).toFixed(1)}%`}
                          >
                            {accuracy > 0 ? `${(accuracy * 100).toFixed(0)}%` : ''}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* color legend */}
          <div className="mt-4 flex items-center gap-2 text-[10px] text-text-muted">
            <span>0%</span>
            <div className="flex h-3 rounded overflow-hidden">
              {Array.from({ length: 10 }).map((_, i) => (
                <div
                  key={i}
                  className="w-4"
                  style={{ backgroundColor: `rgba(100, 0, 117, ${0.15 + (i / 10) * 0.7})` }}
                />
              ))}
            </div>
            <span>100%</span>
          </div>
        </Card>
      )}

      {/* Section 4: Capital Allocation */}
      {allocations && (
        <Card>
          <h4 className="text-sm font-semibold text-text mb-4">Capital Allocation</h4>
          <div className="flex gap-4 text-xs text-text-muted mb-3">
            <div>Regime: <span className="font-bold text-text">{allocations.regime}</span></div>
            {allocations.persistence_probability > 0 && (
              <div>Persistence: <span className="font-bold text-text">{(allocations.persistence_probability * 100).toFixed(1)}%</span></div>
            )}
            {allocations.updated_at && (
              <div>Updated: <span className="text-text">{fmtDate(allocations.updated_at)}</span></div>
            )}
          </div>
          {Object.keys(allocations.allocations).length === 0 ? (
            <p className="text-xs text-text-muted">No allocation data available</p>
          ) : (
            <div className="space-y-2">
              {Object.entries(allocations.allocations)
                .sort(([, a], [, b]) => (b as number) - (a as number))
                .map(([strategy, pct]) => {
                  const value = Number(pct);
                  return (
                    <div key={strategy} className="flex items-center gap-3">
                      <span className="text-xs text-text-muted w-32 truncate font-mono">{strategy}</span>
                      <div className="flex-1 h-5 bg-background rounded overflow-hidden">
                        <div
                          className="h-full rounded transition-all duration-300"
                          style={{ width: `${value * 100}%`, backgroundColor: '#640075' }}
                        />
                      </div>
                      <span className="text-xs text-text w-14 text-right font-mono">{(value * 100).toFixed(1)}%</span>
                    </div>
                  );
                })}
            </div>
          )}
        </Card>
      )}

      {/* Section 5: Anomaly Log */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Anomaly Log</h4>
        {anomalies.length === 0 ? (
          <p className="text-xs text-text-muted">No anomalies detected yet</p>
        ) : (
          <div className="space-y-2">
            {anomalies.map((a, i) => {
              const severityColor =
                a.severity === 'severe' ? '#ef4444'
                : a.severity === 'moderate' ? '#f97316'
                : '#eab308';
              const severityBg =
                a.severity === 'severe' ? 'rgba(239,68,68,0.1)'
                : a.severity === 'moderate' ? 'rgba(249,115,22,0.1)'
                : 'rgba(234,179,8,0.1)';

              return (
                <div key={i} className="flex items-start gap-3 border border-border/30 rounded-lg p-3 hover:bg-surface-hover transition-colors">
                  <span
                    className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full whitespace-nowrap mt-0.5"
                    style={{ backgroundColor: severityBg, color: severityColor }}
                  >
                    {a.severity}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-text">{a.type}</span>
                      {a.ts && <span className="text-[10px] text-text-muted font-mono">{fmtTs(a.ts)}</span>}
                    </div>
                    <p className="text-xs text-text-muted mt-1">{a.message}</p>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   TAB 10: RISK TOOLS (Position Sizing + Correlation + Monte Carlo + Replay)
   ═══════════════════════════════════════════════════════════ */

function RiskToolsTab() {
  const [sizerStatus, setSizerStatus] = useState<Record<string, unknown> | null>(null);
  const [corrStatus, setCorrStatus] = useState<Record<string, unknown> | null>(null);
  const [corrMatrix, setCorrMatrix] = useState<Record<string, unknown> | null>(null);
  const [mcStatus, setMcStatus] = useState<Record<string, unknown> | null>(null);
  const [mcResult, setMcResult] = useState<Record<string, unknown> | null>(null);
  const [mcRunning, setMcRunning] = useState(false);
  const [replayStatus, setReplayStatus] = useState<Record<string, unknown> | null>(null);

  // Position sizing calculator
  const [portfolio, setPortfolio] = useState(10000);
  const [riskPct, setRiskPct] = useState(0.02);
  const [confidence, setConfidence] = useState(0.7);
  const [sizeResult, setSizeResult] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    eventsApi.getPositionSizerStatus().then(r => setSizerStatus(r.data)).catch(() => {});
    eventsApi.getCorrelationStatus().then(r => setCorrStatus(r.data)).catch(() => {});
    eventsApi.getCorrelationMatrix().then(r => setCorrMatrix(r.data)).catch(() => {});
    eventsApi.getMonteCarloStatus().then(r => setMcStatus(r.data)).catch(() => {});
    eventsApi.getReplayStatus().then(r => setReplayStatus(r.data)).catch(() => {});
  }, []);

  const handleCalculateSize = useCallback(() => {
    eventsApi.calculatePositionSize(portfolio, riskPct, confidence)
      .then(r => setSizeResult(r.data))
      .catch(() => {});
  }, [portfolio, riskPct, confidence]);

  const handleRunMC = useCallback(() => {
    setMcRunning(true);
    eventsApi.runMonteCarlo(1000, 252)
      .then(r => { setMcResult(r.data); setMcRunning(false); })
      .catch(() => setMcRunning(false));
  }, []);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Position Sizing Calculator */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Position Sizing (Kelly Criterion)</h4>
        {sizerStatus?.error ? (
          <p className="text-xs text-text-muted mb-3">{String(sizerStatus.error)}</p>
        ) : sizerStatus && (
          <div className="text-xs text-text-muted mb-3">Mode: <span className="text-text font-mono">{String(sizerStatus.mode || 'default')}</span></div>
        )}
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <label className="text-xs text-text-muted w-28">Portfolio $:</label>
            <input
              type="number" value={portfolio} onChange={e => setPortfolio(Number(e.target.value))}
              className="flex-1 bg-background border border-border rounded px-2 py-1 text-xs text-text font-mono"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-text-muted w-28">Risk %:</label>
            <input
              type="number" step="0.005" value={riskPct} onChange={e => setRiskPct(Number(e.target.value))}
              className="flex-1 bg-background border border-border rounded px-2 py-1 text-xs text-text font-mono"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-text-muted w-28">Confidence:</label>
            <input
              type="range" min="0" max="1" step="0.05" value={confidence}
              onChange={e => setConfidence(Number(e.target.value))}
              className="flex-1"
            />
            <span className="text-xs text-text font-mono w-12 text-right">{(confidence * 100).toFixed(0)}%</span>
          </div>
          <button onClick={handleCalculateSize}
            className="w-full px-3 py-2 bg-primary text-white text-xs rounded hover:bg-primary/80 transition-colors">
            Calculate Position Size
          </button>
          {sizeResult && !sizeResult.error && (
            <div className="bg-background rounded p-3 space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-text-muted">Position Size:</span>
                <span className="text-text font-mono font-bold">${Number(sizeResult.position_size || 0).toFixed(2)}</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-text-muted">Risk Amount:</span>
                <span className="text-text font-mono">${Number(sizeResult.risk_amount || 0).toFixed(2)}</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-text-muted">Kelly Fraction:</span>
                <span className="text-text font-mono">{(Number(sizeResult.kelly_fraction || 0) * 100).toFixed(1)}%</span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-text-muted">Confidence Mult:</span>
                <span className="text-text font-mono">{Number(sizeResult.confidence_multiplier || 1).toFixed(2)}x</span>
              </div>
            </div>
          )}
        </div>
      </Card>

      {/* Correlation Guard */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Correlation Guard</h4>
        {corrStatus?.error ? (
          <p className="text-xs text-text-muted">{String(corrStatus.error)}</p>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-3 gap-2">
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{Number(corrStatus?.pairs || 0)}</p>
                <p className="text-[10px] text-text-muted">Pairs</p>
              </div>
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{Number(corrStatus?.violations || 0)}</p>
                <p className="text-[10px] text-text-muted">Violations</p>
              </div>
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{(Number(corrStatus?.max_correlation || 0)).toFixed(2)}</p>
                <p className="text-[10px] text-text-muted">Max Corr</p>
              </div>
            </div>
            {corrMatrix && typeof corrMatrix === 'object' && corrMatrix.matrix && (
              <div className="overflow-x-auto">
                <table className="text-[10px] font-mono w-full">
                  <thead>
                    <tr>
                      <th className="text-left text-text-muted p-1">Pair</th>
                      {(corrMatrix.pairs as string[] || []).map((p: string) => (
                        <th key={p} className="text-center text-text-muted p-1 w-12">{p.slice(0, 5)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(corrMatrix.pairs as string[] || []).map((row: string) => (
                      <tr key={row}>
                        <td className="text-text-muted p-1">{row.slice(0, 8)}</td>
                        {(corrMatrix.pairs as string[] || []).map((col: string) => {
                          const val = ((corrMatrix.matrix as Record<string, Record<string, number>>)?.[row]?.[col]) || 0;
                          const bg = val > 0.7 ? 'rgba(239,68,68,0.2)' : val > 0.4 ? 'rgba(249,115,22,0.15)' : 'transparent';
                          return (
                            <td key={col} className="text-center text-text p-1" style={{ backgroundColor: bg }}>
                              {val.toFixed(2)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Monte Carlo Simulation */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Monte Carlo Simulation</h4>
        {mcStatus?.error ? (
          <p className="text-xs text-text-muted mb-3">{String(mcStatus.error)}</p>
        ) : mcStatus && (
          <div className="text-xs text-text-muted mb-3">
            Simulations run: <span className="text-text font-mono">{Number(mcStatus.simulations || 0)}</span>
          </div>
        )}
        <button onClick={handleRunMC} disabled={mcRunning}
          className="w-full px-3 py-2 bg-primary text-white text-xs rounded hover:bg-primary/80 transition-colors disabled:opacity-50 mb-3">
          {mcRunning ? 'Running...' : 'Run 1000 Simulations (252 days)'}
        </button>
        {mcResult && !mcResult.error && (
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="bg-background rounded p-2">
                <p className="text-[10px] text-text-muted">VaR (95%)</p>
                <p className="text-sm font-bold font-mono" style={{ color: '#ef4444' }}>
                  {(Number(mcResult.var_95 || 0) * 100).toFixed(1)}%
                </p>
              </div>
              <div className="bg-background rounded p-2">
                <p className="text-[10px] text-text-muted">CVaR (95%)</p>
                <p className="text-sm font-bold font-mono" style={{ color: '#ef4444' }}>
                  {(Number(mcResult.cvar_95 || 0) * 100).toFixed(1)}%
                </p>
              </div>
              <div className="bg-background rounded p-2">
                <p className="text-[10px] text-text-muted">Median Return</p>
                <p className="text-sm font-bold font-mono text-text">
                  {(Number(mcResult.median_return || 0) * 100).toFixed(1)}%
                </p>
              </div>
              <div className="bg-background rounded p-2">
                <p className="text-[10px] text-text-muted">Max Drawdown (avg)</p>
                <p className="text-sm font-bold font-mono" style={{ color: '#f97316' }}>
                  {(Number(mcResult.avg_max_drawdown || 0) * 100).toFixed(1)}%
                </p>
              </div>
            </div>
            {mcResult.fan_chart && Array.isArray(mcResult.fan_chart) && (
              <div className="bg-background rounded p-2">
                <p className="text-[10px] text-text-muted mb-1">Fan Chart Percentiles</p>
                <div className="flex items-end gap-0.5 h-16">
                  {(mcResult.fan_chart as { p5: number; p25: number; p50: number; p75: number; p95: number }[])
                    .filter((_: unknown, i: number) => i % Math.max(1, Math.floor((mcResult.fan_chart as unknown[]).length / 50)) === 0)
                    .map((d: { p5: number; p25: number; p50: number; p75: number; p95: number }, i: number) => {
                      const range = Math.max(0.01, d.p95 - d.p5);
                      const mid = ((d.p50 - d.p5) / range) * 100;
                      return (
                        <div key={i} className="flex-1 relative" style={{ height: '100%' }}>
                          <div className="absolute bottom-0 w-full rounded-t" style={{
                            height: `${Math.min(100, range * 200)}%`,
                            background: `linear-gradient(to top, rgba(139,92,246,0.3), rgba(59,130,246,0.3))`,
                          }} />
                          <div className="absolute w-full h-[2px] rounded" style={{
                            bottom: `${Math.min(100, mid)}%`,
                            backgroundColor: '#8b5cf6',
                          }} />
                        </div>
                      );
                    })}
                </div>
                <div className="flex justify-between text-[9px] text-text-muted mt-1">
                  <span>Day 1</span>
                  <span>Day {(mcResult.fan_chart as unknown[]).length}</span>
                </div>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Event Replay */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Event Replay Debugger</h4>
        {replayStatus?.error ? (
          <p className="text-xs text-text-muted">{String(replayStatus.error)}</p>
        ) : (
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{Number(replayStatus?.replays || 0)}</p>
                <p className="text-[10px] text-text-muted">Replays</p>
              </div>
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{Number(replayStatus?.total_trades || 0)}</p>
                <p className="text-[10px] text-text-muted">Trades Analyzed</p>
              </div>
            </div>
            {replayStatus?.last_replay && typeof replayStatus.last_replay === 'object' && (
              <div className="bg-background rounded p-3 space-y-1">
                <p className="text-[10px] text-text-muted uppercase">Last Replay Result</p>
                <div className="flex justify-between text-xs">
                  <span className="text-text-muted">Original PnL:</span>
                  <span className="text-text font-mono">${Number((replayStatus.last_replay as Record<string, unknown>).original_pnl || 0).toFixed(2)}</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-text-muted">Replayed PnL:</span>
                  <span className="text-text font-mono">${Number((replayStatus.last_replay as Record<string, unknown>).replayed_pnl || 0).toFixed(2)}</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-text-muted">Delta:</span>
                  <span className="font-mono font-bold" style={{
                    color: Number((replayStatus.last_replay as Record<string, unknown>).pnl_delta || 0) >= 0 ? '#10b981' : '#ef4444'
                  }}>
                    ${Number((replayStatus.last_replay as Record<string, unknown>).pnl_delta || 0).toFixed(2)}
                  </span>
                </div>
              </div>
            )}
            <p className="text-[10px] text-text-muted">Counterfactual analysis: "What if the committee was active during past trades?"</p>
          </div>
        )}
      </Card>
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════
   TAB 11: EVOLUTION (Genetic Algorithm)
   ═══════════════════════════════════════════════════════════ */

function EvolutionTab() {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [best, setBest] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    eventsApi.getEvolutionStatus().then(r => setStatus(r.data)).catch(() => {});
    eventsApi.getEvolutionBest().then(r => setBest(r.data)).catch(() => {});
  }, []);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Evolution Status */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Genetic Algorithm Status</h4>
        {status?.error ? (
          <p className="text-xs text-text-muted">{String(status.error)}</p>
        ) : status && (
          <div className="space-y-3">
            <div className="grid grid-cols-3 gap-2">
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{Number(status.generations || 0)}</p>
                <p className="text-[10px] text-text-muted">Generations</p>
              </div>
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-text">{Number(status.population_size || 0)}</p>
                <p className="text-[10px] text-text-muted">Population</p>
              </div>
              <div className="bg-background rounded p-2 text-center">
                <p className="text-lg font-bold text-profit">{Number(status.best_fitness || 0).toFixed(4)}</p>
                <p className="text-[10px] text-text-muted">Best Fitness</p>
              </div>
            </div>

            {/* Fitness History Chart */}
            {status.fitness_history && Array.isArray(status.fitness_history) && (status.fitness_history as number[]).length > 1 && (
              <div className="bg-background rounded p-3">
                <p className="text-[10px] text-text-muted uppercase mb-2">Fitness Over Generations</p>
                <div className="flex items-end gap-0.5 h-20">
                  {(status.fitness_history as number[]).map((f: number, i: number) => {
                    const max = Math.max(...(status.fitness_history as number[]));
                    const min = Math.min(...(status.fitness_history as number[]));
                    const range = max - min || 1;
                    const pct = ((f - min) / range) * 100;
                    return (
                      <div key={i} className="flex-1 rounded-t transition-all"
                        style={{
                          height: `${Math.max(2, pct)}%`,
                          backgroundColor: `rgba(16,185,129,${0.3 + (pct / 100) * 0.7})`,
                        }}
                        title={`Gen ${i + 1}: ${f.toFixed(4)}`}
                      />
                    );
                  })}
                </div>
                <div className="flex justify-between text-[9px] text-text-muted mt-1">
                  <span>Gen 1</span>
                  <span>Gen {(status.fitness_history as number[]).length}</span>
                </div>
              </div>
            )}

            {status.mutation_rate !== undefined && (
              <div className="flex gap-4 text-xs text-text-muted">
                <span>Mutation: <span className="text-text font-mono">{(Number(status.mutation_rate || 0) * 100).toFixed(1)}%</span></span>
                <span>Crossover: <span className="text-text font-mono">{(Number(status.crossover_rate || 0) * 100).toFixed(1)}%</span></span>
                <span>Elite: <span className="text-text font-mono">{Number(status.elite_size || 0)}</span></span>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Best Genome */}
      <Card>
        <h4 className="text-sm font-semibold text-text mb-4">Best Genome</h4>
        {best?.error ? (
          <p className="text-xs text-text-muted">{String(best.error)}</p>
        ) : best?.best && typeof best.best === 'object' ? (
          <div className="space-y-3">
            <div className="bg-background rounded p-3">
              <div className="flex justify-between items-center mb-2">
                <span className="text-xs text-text-muted">Fitness Score</span>
                <span className="text-lg font-bold text-profit font-mono">
                  {Number((best.best as Record<string, unknown>).fitness || 0).toFixed(4)}
                </span>
              </div>
              <div className="flex justify-between items-center mb-2">
                <span className="text-xs text-text-muted">Generation</span>
                <span className="text-sm text-text font-mono">
                  {Number((best.best as Record<string, unknown>).generation || 0)}
                </span>
              </div>
            </div>

            {/* Parameters */}
            {(best.best as Record<string, unknown>).parameters && typeof (best.best as Record<string, unknown>).parameters === 'object' && (
              <div>
                <p className="text-[10px] text-text-muted uppercase mb-2">Optimized Parameters</p>
                <div className="space-y-1.5">
                  {Object.entries((best.best as Record<string, unknown>).parameters as Record<string, unknown>)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([key, value]) => (
                      <div key={key} className="flex items-center gap-2">
                        <span className="text-xs text-text-muted w-40 truncate font-mono">{key}</span>
                        <div className="flex-1 h-1.5 bg-background rounded overflow-hidden">
                          <div className="h-full rounded bg-primary/60" style={{ width: `${Math.min(100, Number(value) * 10)}%` }} />
                        </div>
                        <span className="text-xs text-text w-16 text-right font-mono">
                          {typeof value === 'number' ? value.toFixed(4) : String(value)}
                        </span>
                      </div>
                    ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <p className="text-xs text-text-muted">No evolution data yet. Run the genetic algorithm to see results.</p>
        )}
      </Card>
    </div>
  );
}


/* ─────────────────── shared ─────────────────── */

function KV({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <span className="text-text-muted text-[10px] uppercase tracking-wider">{label}</span>
      <div className={`text-text text-xs mt-0.5 break-all ${mono ? 'font-mono' : ''}`}>{value}</div>
    </div>
  );
}
