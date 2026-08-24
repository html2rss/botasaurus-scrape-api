import ipaddress
import unittest

from app.security import UrlGuard


class UrlGuardUnitTests(unittest.TestCase):
    def test_validate_rejects_non_http_schemes(self):
        res = UrlGuard.validate("ftp://example.com/file")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Only http/https", res.error_message)

    def test_validate_rejects_missing_hostname(self):
        res = UrlGuard.validate("http://")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 400)

    def test_validate_rejects_localhost(self):
        res = UrlGuard.validate("http://localhost:8080/test")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 403)

    def test_validate_proxy_rejects_blocked_host(self):
        res = UrlGuard.validate_proxy("http://127.0.0.1:8080")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 403)
        self.assertIn("Proxy URL is invalid or blocked", res.error_message)

    def test_is_blocked_ip_allows_well_known_nat64_prefix(self):
        nat64_ip = ipaddress.ip_address("64:ff9b::3691:8e03")
        self.assertFalse(UrlGuard.is_blocked_ip(nat64_ip))

    def test_is_blocked_ip_still_blocks_loopback(self):
        loopback = ipaddress.ip_address("127.0.0.1")
        self.assertTrue(UrlGuard.is_blocked_ip(loopback))
