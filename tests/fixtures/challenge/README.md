# Challenge corpus

Shared HTML fixtures for Botasaurus scrape-api `ChallengeDetector` and the html2rss gem `BlockedSurface` module.

**Owner:** `botasaurus-scrape-api/tests/fixtures/challenge/`
**Consumers:** scrape-api unit tests; gem `blocked_surface_spec` loads these files via the sibling path under the org workspace.

Do not duplicate interstitial HTML in gem specs — assert against these files.
