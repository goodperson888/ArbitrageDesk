"""Explicit Lighter deployment parameters.

The two deployments share the SDK protocol but not the REST origin,
settlement asset, or L2 signing domain.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LighterDeployment:
    name: str
    rest_url: str
    ws_url: str
    signing_chain_id: int
    settlement_asset: str


CORE = LighterDeployment(
    name="lighter", rest_url="https://mainnet.zklighter.elliot.ai",
    ws_url="wss://mainnet.zklighter.elliot.ai/stream", signing_chain_id=304,
    settlement_asset="USDC",
)
ROBINHOOD = LighterDeployment(
    name="lighter_rh", rest_url="https://api.rh.lighter.xyz",
    ws_url="wss://api.rh.lighter.xyz/stream", signing_chain_id=466324,
    settlement_asset="USDG",
)


DEPLOYMENTS = {CORE.name: CORE, ROBINHOOD.name: ROBINHOOD}

