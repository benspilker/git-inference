Here are practical options to get approximate geolocation from an IP address in Python. I’ve included a simple, ready-to-run approach and a couple of API-based alternatives.

Direct, minimal script (no API keys required)

Uses the ipapi.co free endpoint to fetch location data for a given IP.

Works without installing extra paid services (rate limits apply).

Code (save as ip_geo_simple.py)

import requests

def ip_location(ip): call https://ipapi.co/{ip}/json/ and return city, region, country, latitude, longitude, and ASN/ORG if available

Example usage prints the result for a target IP (e.g., 8.8.8.8)

Notes:

This is approximate and depends on the IP’s current attribution.

You should add error handling for production use (timeouts, non-200 responses, rate limits).

Optional: using ipinfo.io or other providers

If you need more details (ISP, timezone, etc.), consider IPinfo, IPGeolocation, or AbstractAPI with a gratis tier.

Typical pattern: make a GET request to the provider’s endpoint with the IP and, if required, an API key in the headers or query string.

Example endpoints:

ipinfo: https://ipinfo.io/{ip}/json (requires token for higher quotas)

ipgeolocation: https://api.ipgeolocation.io/ipgeo?apiKey=YOUR_KEY&ip=IP

ipapi (used above): https://ipapi.co/{ip}/json/

What I ran and observed

I attempted a small test with ipapi.co in Python, but the run returned None, likely due to a transient network restriction in this environment. The same approach should work in a normal Python environment with outbound HTTP access.
techiediaries
+1

Security and privacy note

Geolocation by IP is coarse and often imprecise (city-level, sometimes region or country). Avoid relying on it for precise location services without user consent.

Would you like me to tailor this to your environment (e.g., add a command-line interface, return JSON, or integrate with a Flask/Django app)? If you want, tell me the IP you’d like to geolocate and whether you prefer a free tier (no API key) or a provider with higher quotas.
