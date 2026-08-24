from collections.abc import Generator
from typing import Any

from botasaurus_driver import cdp as cdp

class CdpCommandGenerator(Generator[dict[str, Any], dict[str, Any], Any]):
    pass

class CdpTab:
    def send(self, cdp_obj: CdpCommandGenerator) -> Any: ...
    def after_response_received(self, handler: Any, /) -> None: ...
    def add_handler(self, event_type: type[Any], handler: Any, /) -> None: ...

def enable_network() -> CdpCommandGenerator: ...
