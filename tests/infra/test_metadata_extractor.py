import unittest
from typing import cast

from app.engine.driver_protocol import DriverProtocol
from app.infra.metadata import MetadataExtractor


class MetadataExtractorUnitTests(unittest.TestCase):
    def test_extract_passive_metadata_from_requests_list(self):
        class _Req:
            def __init__(self, status: int, headers: dict[str, str], url: str) -> None:
                self.response = type(
                    "Resp", (), {"status_code": status, "headers": headers}
                )()
                self.url = url

        driver = type(
            "D",
            (),
            {
                "requests": [
                    _Req(
                        200, {"content-type": "text/html"}, "https://example.com/final"
                    )
                ]
            },
        )()
        status, headers, final_url = MetadataExtractor.extract_from_requests(
            cast(DriverProtocol, driver)
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"content-type": "text/html"})
        self.assertEqual(final_url, "https://example.com/final")

    def test_extract_falls_back_to_200_when_no_driver_metadata(self):
        driver = type("EmptyDriver", (), {"current_url": "https://example.com/dest"})()
        meta = MetadataExtractor.fetch(
            cast(DriverProtocol, driver), "https://example.com"
        )
        self.assertEqual(meta.status_code, 200)
        self.assertEqual(meta.final_url, "https://example.com/dest")
        self.assertIsNone(meta.headers)
        self.assertIsNone(meta.metadata_error)
