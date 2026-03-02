"""
engine/client_manager.py
─────────────────────────
Initialise and expose the py-clob-client ClobClient singleton.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from py_clob_client.client import ClobClient

from .config import CLOB_HOST, Config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _build_client(
    private_key: str,
    chain_id: int,
    signature_type: int,
    funder: str,
) -> ClobClient:
    """Cache-and-return a single ClobClient instance (thread-safe singleton via lru_cache)."""
    client = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=chain_id,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    logger.info("ClobClient initialised for funder %s (chain %d)", funder, chain_id)
    return client


def get_client(cfg: Config) -> ClobClient:
    """Return the authenticated ClobClient, creating it once per process."""
    return _build_client(
        private_key=cfg.private_key,
        chain_id=cfg.chain_id,
        signature_type=cfg.signature_type,
        funder=cfg.funder_address,
    )


def get_readonly_client() -> ClobClient:
    """Return an unauthenticated (read-only) ClobClient — no private key needed."""
    return ClobClient(CLOB_HOST)
