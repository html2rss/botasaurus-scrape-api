import asyncio
import json
import logging
import unittest

from starlette.requests import Request

from app.api.errors import unhandled_exception_handler
from app.logging_config import LOGGER_NAME, JsonFormatter, setup_logging
from tests.infra.test_logging_config import io


class UnhandledExceptionLogTests(unittest.TestCase):
    def test_logs_structured_error_with_stack_and_request_id(self) -> None:
        setup_logging(log_format="json")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(handler)
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/scrape",
                "headers": [(b"x-request-id", b"req-500")],
            }
        )
        try:
            try:
                raise RuntimeError("kaboom")
            except RuntimeError as exc:
                response = asyncio.run(unhandled_exception_handler(request, exc))
        finally:
            logger.removeHandler(handler)
        self.assertEqual(response.status_code, 500)
        payload = json.loads(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["event_name"], "unhandled_exception")
        self.assertEqual(payload["level"], "error")
        self.assertEqual(payload["request_id"], "req-500")
        self.assertEqual(payload["exc_type"], "RuntimeError")
        self.assertIn("kaboom", payload["exc_stack"])


if __name__ == "__main__":
    unittest.main()
