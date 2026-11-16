"""Credential-backed execution client factories for Lighter deployments.

This module is deliberately side-effect free until a caller explicitly asks
for a session.  It centralizes the deployment URL and signing-domain mapping
so RH cannot accidentally reuse Core-mainnet parameters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.lighter.api_trader import LighterApiTrader
from src.lighter.deployment import CORE, ROBINHOOD, LighterDeployment
from src.lighter.execution import OfficialSDKTransport, SDKLoop


@dataclass(frozen=True)
class LighterCredentials:
    account_index: int
    key_index: int
    private_key: str


def deployment_for(venue: str) -> LighterDeployment:
    if venue == 'lighter':
        return CORE
    if venue == 'lighter_rh':
        return ROBINHOOD
    raise ValueError('未知 Lighter 部署')


def build_lighter_session(venue: str, credentials: LighterCredentials,
                          proxy_url: Optional[str] = None):
    """Return ``(public_client, sdk_loop)`` for an explicitly selected venue."""
    deployment = deployment_for(venue)
    config = {'api': {'base_url': deployment.rest_url, 'ws_url': deployment.ws_url,
                      'proxy_url': proxy_url,
                      'request_timeout': 30}}
    public = LighterApiTrader(config, account_index=credentials.account_index)
    sdk = SDKLoop(lambda: OfficialSDKTransport.from_credentials(
        credentials.account_index, credentials.key_index, credentials.private_key,
        url=deployment.rest_url, proxy_url=proxy_url,
        chain_id=deployment.signing_chain_id))
    return public, sdk

