"""Event-runtime validation without redundant chain reads on untagged calls.

The private helpers below match the Web3 7.16 implementation in uv.lock; offline
HTTP tests compare behavior with its default middleware when dependencies change.
Explicit startup/executor chain guards remain required. No chain ID is cached.
"""

from web3.middleware import Web3Middleware
from web3.middleware.formatting import FormattingMiddlewareBuilder
from web3.middleware.validation import (
    METHODS_TO_VALIDATE,
    _build_formatters_dict,
    _chain_id_validator,
)


class EventValidationMiddleware(Web3Middleware):
    def __init__(self, w3):
        super().__init__(w3)
        # Keep upstream block validation and response handling, without its dynamic
        # builder (which reads chainId again while processing call responses).
        self._responses = FormattingMiddlewareBuilder.build(
            w3, **_build_formatters_dict({})
        )

    def request_processor(self, method, params):
        if method in METHODS_TO_VALIDATE and "chainId" in params[0]:
            # Presence, not truthiness: null/malformed declarations must still pass
            # through upstream validation before chainId is removed from RPC params.
            params = _chain_id_validator(self._w3.eth.chain_id)(params)
        return method, params

    def response_processor(self, method, response):
        return self._responses.response_processor(method, response)
