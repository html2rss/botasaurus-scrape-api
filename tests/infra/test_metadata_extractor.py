import unittest

from app.infra.metadata import DocumentResponse, resolve_metadata


class ResolveMetadataTests(unittest.TestCase):
    def test_uses_captured_document_status_and_headers(self) -> None:
        doc = DocumentResponse(403, {"x-datadome": "protected"}, "https://e.com/")
        meta = resolve_metadata(doc, "https://e.com/final", "https://e.com")
        self.assertEqual(meta.status_code, 403)
        self.assertEqual(meta.headers, {"x-datadome": "protected"})
        self.assertEqual(meta.final_url, "https://e.com/final")

    def test_defaults_to_200_without_document(self) -> None:
        meta = resolve_metadata(None, None, "https://e.com")
        self.assertEqual(meta.status_code, 200)
        self.assertIsNone(meta.headers)
        self.assertEqual(meta.final_url, "https://e.com")


if __name__ == "__main__":
    unittest.main()
