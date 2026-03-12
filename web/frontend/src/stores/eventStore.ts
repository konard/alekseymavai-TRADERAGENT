import { create } from 'zustand';

export interface DomainEvent {
  event_id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  data: Record<string, unknown>;
  ts: number;
  causes: string[];
  enables: string[];
  bot_name: string;
  priority: number;
}

interface EventState {
  events: DomainEvent[];
  filteredEntityType: string | null;
  isPaused: boolean;
  maxEvents: number;

  addEvent: (event: DomainEvent) => void;
  setFilter: (entityType: string | null) => void;
  togglePause: () => void;
  clearEvents: () => void;
}

export const useEventStore = create<EventState>((set) => ({
  events: [],
  filteredEntityType: null,
  isPaused: false,
  maxEvents: 500,

  addEvent: (event) =>
    set((state) => {
      if (state.isPaused) return state;
      const events = [event, ...state.events].slice(0, state.maxEvents);
      return { events };
    }),

  setFilter: (entityType) => set({ filteredEntityType: entityType }),
  togglePause: () => set((state) => ({ isPaused: !state.isPaused })),
  clearEvents: () => set({ events: [] }),
}));
