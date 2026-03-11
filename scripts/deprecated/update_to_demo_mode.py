#!/usr/bin/env python3
"""
Update credentials to Demo Trading mode (testnet).

Demo Trading configuration:
- testnet=True (uses api-demo.bybit.com)
- category='linear' (futures, NOT spot!)
- accountType='UNIFIED'
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot.database.manager import DatabaseManager


async def update_to_demo():
    """Update credentials to Demo Trading (testnet=True)"""

    print("=" * 70)
    print("Update ByBit Credentials to Demo Trading Mode")
    print("=" * 70)
    print()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("❌ DATABASE_URL not set!")
        sys.exit(1)

    db = DatabaseManager(database_url)
    await db.initialize()

    cred = await db.get_credentials_by_name("bybit_production")
    if not cred:
        print("❌ Credentials 'bybit_production' not found!")
        sys.exit(1)

    print(f"Current settings:")
    print(f"  • Name: {cred.name}")
    print(f"  • Exchange: {cred.exchange_id}")
    print(f"  • Sandbox: {cred.is_sandbox}")
    print(f"  • Active: {cred.is_active}")
    print()

    # Update to Demo Trading (testnet=True)
    cred.is_sandbox = True

    await db.update(cred)

    print("✅ Updated to Demo Trading mode!")
    print(f"  • Sandbox: {cred.is_sandbox}")
    print()
    print("Demo Trading configuration:")
    print("  • Base URL: https://api-demo.bybit.com")
    print("  • Market Type: linear (futures)")
    print("  • Account Type: UNIFIED")
    print("  • Virtual funds: USDT 100,000 + BTC 1 + ETH 1")
    print()

    await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(update_to_demo())
    except KeyboardInterrupt:
        print("\n❌ Interrupted")
        sys.exit(1)
