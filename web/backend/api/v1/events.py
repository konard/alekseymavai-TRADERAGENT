"""Event ontology API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query

from bot.core.event_bus import EventBus, DomainEvent

# event_registry and reducers may not exist yet — import gracefully
try:
    from bot.core.event_registry import (
        EVENT_REGISTRY,
        get_event_meta,
        get_valid_transitions,
        get_entity_events,
    )
except ImportError:
    EVENT_REGISTRY: dict = {}

    def get_event_meta(event_type: str) -> dict | None:
        return None

    def get_valid_transitions(event_type: str) -> list:
        return []

    def get_entity_events(entity_type: str) -> list:
        return []

try:
    from bot.core.reducers import REDUCERS, get_state_at
except ImportError:
    REDUCERS: dict = {}

    def get_state_at(
        events: list[DomainEvent],
        entity_type: str,
        timestamp: float | None = None,
    ) -> dict:
        """Fallback: return last event's data as state."""
        if not events:
            return {}
        if timestamp is not None:
            events = [e for e in events if e.ts <= timestamp]
        return events[-1].data if events else {}


router = APIRouter(prefix="/api/v1/events", tags=["events"])

# Global event bus singleton (will be set by app lifespan)
_event_bus: EventBus | None = None


def set_event_bus(bus: EventBus) -> None:
    """Set the global event bus instance (called during app startup)."""
    global _event_bus
    _event_bus = bus


def get_bus() -> EventBus:
    """Get the active event bus, or a dummy instance for standalone web mode."""
    if _event_bus is None:
        # Return a dummy bus for when no bot is running
        return EventBus(bot_name="web")
    return _event_bus


def _event_to_dict(event: DomainEvent) -> dict:
    """Safely serialize a DomainEvent to a JSON-compatible dict."""
    return {
        "event_id": event.event_id,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "event_type": event.event_type,
        "data": event.data,
        "ts": event.ts,
        "causes": event.causes,
        "enables": event.enables,
        "bot_name": event.bot_name,
        "priority": event.priority.value if hasattr(event.priority, "value") else event.priority,
    }


# ── Timeline ────────────────────────────────────────────────────────────────


@router.get("/timeline/{entity_type}/{entity_id}")
async def get_timeline(entity_type: str, entity_id: str):
    """Get event timeline for a specific entity."""
    bus = get_bus()
    events = bus.get_timeline(entity_type, entity_id)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "events": [_event_to_dict(e) for e in events],
        "count": len(events),
    }


# ── All events ──────────────────────────────────────────────────────────────


@router.get("/all")
async def get_all_events(
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    since: float | None = Query(None),
):
    """Get all events with pagination."""
    bus = get_bus()
    if since is not None:
        events = bus.get_events_since(since)
    else:
        events = bus.get_all_events(limit=limit, offset=offset)
    return {
        "events": [_event_to_dict(e) for e in events],
        "count": len(events),
    }


# ── Entity state (reducer projection) ──────────────────────────────────────


@router.get("/state/{entity_type}/{entity_id}")
async def get_entity_state(
    entity_type: str,
    entity_id: str,
    at: float | None = Query(None),
):
    """Get projected state of an entity (using reducers)."""
    bus = get_bus()
    events = bus.get_timeline(entity_type, entity_id)
    state = get_state_at(events, entity_type, timestamp=at)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "state": state,
        "events_count": len(events),
    }


# ── Registry ────────────────────────────────────────────────────────────────


@router.get("/registry")
async def get_registry():
    """Get full event registry (all event type definitions)."""
    return {"registry": EVENT_REGISTRY, "count": len(EVENT_REGISTRY)}


@router.get("/registry/{event_type}")
async def get_event_info(event_type: str):
    """Get metadata for a specific event type."""
    meta = get_event_meta(event_type)
    if not meta:
        return {"error": f"Unknown event type: {event_type}"}
    return {
        "event_type": event_type,
        "meta": meta,
        "transitions": get_valid_transitions(event_type),
        "causes": [],
    }


# ── Entity types ────────────────────────────────────────────────────────────


@router.get("/entities/{entity_type}")
async def get_entity_type_events(entity_type: str):
    """Get all event types for an entity type."""
    events = get_entity_events(entity_type)
    return {
        "entity_type": entity_type,
        "event_types": events,
        "count": len(events),
    }


# ── Graph visualisation data ────────────────────────────────────────────────


@router.get("/graph")
async def get_event_graph(
    entity_type: str | None = Query(None),
    entity_id: str | None = Query(None),
    limit: int = Query(200, le=1000),
):
    """Get event graph data for visualization (nodes + edges)."""
    bus = get_bus()

    if entity_type and entity_id:
        events = bus.get_timeline(entity_type, entity_id)
    else:
        events = bus.get_all_events(limit=limit)

    nodes: list[dict] = []
    edges: list[dict] = []
    event_ids: set[str] = set()

    for evt in events:
        meta = get_event_meta(evt.event_type)
        label = meta.get("label", evt.event_type) if meta else evt.event_type

        node = {
            "id": evt.event_id,
            "type": "event",
            "label": label,
            "entity_type": evt.entity_type,
            "entity_id": evt.entity_id,
            "event_type": evt.event_type,
            "ts": evt.ts,
            "data": evt.data,
        }
        nodes.append(node)
        event_ids.add(evt.event_id)

        # Add causal edges
        for cause_id in (evt.causes or []):
            if cause_id in event_ids:
                edges.append({
                    "source": cause_id,
                    "target": evt.event_id,
                    "relation": "causes",
                })

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# ── Statistics ──────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_event_stats():
    """Get event statistics."""
    bus = get_bus()
    all_events = bus.get_all_events(limit=10000)

    by_entity: dict[str, int] = {}
    by_event_type: dict[str, int] = {}
    for evt in all_events:
        by_entity[evt.entity_type] = by_entity.get(evt.entity_type, 0) + 1
        by_event_type[evt.event_type] = by_event_type.get(evt.event_type, 0) + 1

    return {
        "total_events": len(all_events),
        "by_entity_type": by_entity,
        "by_event_type": by_event_type,
        "registry_size": len(EVENT_REGISTRY),
    }


# ── Transition matrix ──────────────────────────────────────────────────────


@router.get("/transition-matrix")
async def get_transition_matrix(entity_type: str | None = Query(None)):
    """Get transition probability matrix between event types."""
    bus = get_bus()
    all_events = bus.get_all_events(limit=10000)

    if entity_type:
        all_events = [e for e in all_events if e.entity_type == entity_type]

    # Group by entity and sort by time
    entity_timelines: dict[str, list[DomainEvent]] = {}
    for evt in all_events:
        key = f"{evt.entity_type}:{evt.entity_id}"
        entity_timelines.setdefault(key, []).append(evt)

    # Build transition counts
    transitions: dict[str, int] = {}
    for timeline in entity_timelines.values():
        sorted_tl = sorted(timeline, key=lambda e: e.ts)
        for i in range(len(sorted_tl) - 1):
            from_type = sorted_tl[i].event_type
            to_type = sorted_tl[i + 1].event_type
            tkey = f"{from_type}->{to_type}"
            transitions[tkey] = transitions.get(tkey, 0) + 1

    # Convert to probabilities
    from_totals: dict[str, int] = {}
    for tkey, count in transitions.items():
        from_type = tkey.split("->")[0]
        from_totals[from_type] = from_totals.get(from_type, 0) + count

    matrix: dict[str, dict] = {}
    for tkey, count in transitions.items():
        from_type = tkey.split("->")[0]
        total = from_totals.get(from_type, 1)
        matrix[tkey] = {"count": count, "probability": round(count / total, 4)}

    return {
        "matrix": matrix,
        "total_transitions": sum(transitions.values()),
    }


# ── Committee ─────────────────────────────────────────────────────────────

# Global singletons (set by app lifespan)
_committee = None
_automation = None
_predictive_model = None
_pattern_learner = None


def set_committee(committee) -> None:
    global _committee
    _committee = committee


def set_automation(automation) -> None:
    global _automation
    _automation = automation


def set_predictive_model(model) -> None:
    global _predictive_model
    _predictive_model = model


def set_pattern_learner(learner) -> None:
    global _pattern_learner
    _pattern_learner = learner


@router.get("/committee/status")
async def get_committee_status():
    """Get Investment Committee status and history."""
    if not _committee:
        return {"error": "Committee not initialized", "experts": [], "total_sessions": 0}
    return _committee.get_status()


@router.get("/committee/history")
async def get_committee_history(limit: int = Query(20, le=100)):
    """Get recent committee decisions."""
    if not _committee:
        return {"decisions": [], "count": 0}
    decisions = _committee.history[:limit]
    return {
        "decisions": [d.to_dict() for d in decisions],
        "count": len(decisions),
    }


@router.get("/automation/status")
async def get_automation_status():
    """Get automation rules status."""
    if not _automation:
        return {"error": "Automation not initialized", "rules": [], "total_rules": 0}
    return _automation.get_status()


@router.get("/automation/history")
async def get_automation_history(limit: int = Query(20, le=100)):
    """Get recent automation firing history."""
    if not _automation:
        return {"history": [], "count": 0}
    history = _automation.history[:limit]
    return {"history": history, "count": len(history)}


@router.get("/predictions/status")
async def get_predictions_status():
    """Get predictive model status."""
    if not _predictive_model:
        return {"error": "Predictive model not initialized", "total_states": 0}
    return _predictive_model.get_stats()


@router.get("/predictions/predict")
async def predict_next(
    current_state: str = Query(...),
    prev_state: str | None = Query(None),
):
    """Predict next state from current."""
    if not _predictive_model:
        return {"error": "Predictive model not initialized"}
    pred = _predictive_model.predict_next(current_state, prev_state)
    return pred.to_dict()


@router.get("/predictions/matrix")
async def get_markov_matrix():
    """Get Markov transition matrix."""
    if not _predictive_model:
        return {"matrix": {}, "stationary": {}}
    return {
        "matrix": _predictive_model.get_transition_matrix(),
        "stationary": _predictive_model.get_stationary_distribution(),
    }


@router.get("/patterns/status")
async def get_patterns_status():
    """Get pattern learner status."""
    if not _pattern_learner:
        return {"error": "Pattern learner not initialized", "total_patterns": 0}
    return _pattern_learner.get_stats()


@router.get("/patterns/top")
async def get_top_patterns(n: int = Query(20, le=100)):
    """Get top patterns by confidence."""
    if not _pattern_learner:
        return {"patterns": [], "count": 0}
    patterns = _pattern_learner.get_top_patterns(n=n)
    return {"patterns": patterns, "count": len(patterns)}
