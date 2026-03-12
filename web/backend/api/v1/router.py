"""
Aggregate v1 API router.
"""

from fastapi import APIRouter

from web.backend.api.v1.backtesting import router as backtesting_router
from web.backend.api.v1.bots import router as bots_router
from web.backend.api.v1.dashboard import router as dashboard_router
from web.backend.api.v1.market import router as market_router
from web.backend.api.v1.portfolio import router as portfolio_router
from web.backend.api.v1.settings import router as settings_router
from web.backend.api.v1.strategies import router as strategies_router
from web.backend.api.v1.events import router as events_router

v1_router = APIRouter()
v1_router.include_router(bots_router)
v1_router.include_router(dashboard_router)
v1_router.include_router(strategies_router)
v1_router.include_router(portfolio_router)
v1_router.include_router(backtesting_router)
v1_router.include_router(market_router)
v1_router.include_router(settings_router)
v1_router.include_router(events_router)
